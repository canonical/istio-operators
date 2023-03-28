#!/usr/bin/env python3

import logging
import subprocess

import yaml
from charmed_kubeflow_chisme.exceptions import GenericCharmRuntimeError
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from jinja2 import Environment, FileSystemLoader
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.resources.admissionregistration_v1 import ValidatingWebhookConfiguration
from lightkube.resources.core_v1 import Service
from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from packaging.version import Version
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces

from istio_gateway_info_provider import RELATION_NAME, GatewayProvider
from istioctl import Istioctl, IstioctlError
from resources_handler import ResourceHandler

GATEWAY_HTTP_PORT = 8080
GATEWAY_HTTPS_PORT = 8443
METRICS_PORT = 15014
GATEWAY_WORKLOAD_SERVICE_NAME = "istio-ingressgateway-workload"
ISTIOCTL_PATH = "./istioctl"
ISTIOCTL_DEPOYMENT_PROFILE = "minimal"
UPGRADE_FAILED_MSG = (
    "Failed to upgrade Istio.  {message}  To recover Istio, see [the upgrade docs]"
    "(https://github.com/canonical/istio-operators/blob/main/charms/istio-pilot/README.md) for "
    "recommendations."
)

UPGRADE_FAILED_VERSION_ERROR_MSG = (
    "Failed to upgrade Istio because of an error retrieving version information about istio.  "
    "Got message: '{message}' when trying to retrieve version information.  To recover Istio, see"
    " [the upgrade docs]"
    "(https://github.com/canonical/istio-operators/blob/main/charms/istio-pilot/README.md) for "
    "recommendations."
)


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        # TODO: https://github.com/canonical/istio-operators/issues/196
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return
        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            self.model.unit.status = WaitingStatus(str(err))
            return
        except NoCompatibleVersions as err:
            self.model.unit.status = BlockedStatus(str(err))
            return
        else:
            self.model.unit.status = ActiveStatus()

        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")

        if self._istiod_svc:
            self._scraping = MetricsEndpointProvider(
                self,
                relation_name="metrics-endpoint",
                jobs=[{"static_configs": [{"targets": [f"{self._istiod_svc}:{METRICS_PORT}"]}]}],
            )
        self.grafana_dashboards = GrafanaDashboardProvider(self, relation_name="grafana-dashboard")
        self.log = logging.getLogger(__name__)

        self.env = Environment(loader=FileSystemLoader("src"))
        self._resource_handler = ResourceHandler(self.app.name, self.model.name)

        self._resource_files = [
            "gateway.yaml.j2",
            "auth_filter.yaml.j2",
            "virtual_service.yaml.j2",
        ]
        self.gateway = GatewayProvider(self)

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)
        self.framework.observe(self.on.upgrade_charm, self.upgrade_charm)

        for event in [self.on.config_changed, self.on["ingress"].relation_created]:
            self.framework.observe(event, self.handle_default_gateway)

        # FIXME: Calling handle_gateway_relation on update_status ensures gateway information is
        # sent eventually to the related units, this is temporal and we should find a way to
        # ensure all event handlers are called when they are supposed to.
        for event in [self.on[RELATION_NAME].relation_changed, self.on.update_status]:
            self.framework.observe(event, self.handle_gateway_info_relation)
        self.framework.observe(self.on["istio-pilot"].relation_changed, self.send_info)
        self.framework.observe(self.on["ingress"].relation_changed, self.handle_ingress)
        self.framework.observe(self.on["ingress"].relation_broken, self.handle_ingress)
        self.framework.observe(self.on["ingress"].relation_departed, self.handle_ingress)
        self.framework.observe(self.on["ingress-auth"].relation_changed, self.handle_ingress_auth)
        self.framework.observe(self.on["ingress-auth"].relation_departed, self.handle_ingress_auth)

    def install(self, event):
        """Install charm."""
        self._log_and_set_status(MaintenanceStatus("Deploying Istio control plane"))

        subprocess.check_call(
            [
                "./istioctl",
                "install",
                "-y",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={self.model.name}",
            ]
        )

        # Patch any known issues with the install
        # Istioctl v1.12-1.14 have a bug where the validating webhook is not deployed to the
        # correct namespace (see https://github.com/canonical/istio-operators/issues/204)
        # This has no effect if the webhook does not exist or is already correct
        self._log_and_set_status(MaintenanceStatus("Patching webhooks"))
        self._patch_istio_validating_webhook()

        self.unit.status = ActiveStatus()

    def remove(self, event):
        """Remove charm."""

        manifests = subprocess.check_output(
            [
                "./istioctl",
                "manifest",
                "generate",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={self.model.name}",
            ]
        )

        custom_resource_classes = [
            self._resource_handler.get_custom_resource_class_from_filename(resource_file)
            for resource_file in self._resource_files
        ]
        for resource in custom_resource_classes:
            self._resource_handler.delete_existing_resources(
                resource, namespace=self.model.name, ignore_unauthorized=True
            )
        self._resource_handler.delete_manifest(
            manifests, namespace=self.model.name, ignore_not_found=True, ignore_unauthorized=True
        )

    def upgrade_charm(self, event):
        """Upgrade charm.

        Supports upgrade of exactly one minor version at a time.
        """
        istioctl = Istioctl(ISTIOCTL_PATH, self.model.name, ISTIOCTL_DEPOYMENT_PROFILE)
        self._log_and_set_status(MaintenanceStatus("Upgrading Istio"))

        # Check for version compatibility for the upgrade
        try:
            versions = istioctl.version()
        except IstioctlError as e:
            self.log.error(UPGRADE_FAILED_MSG.format(message=str(e)))
            raise GenericCharmRuntimeError(
                "Failed to upgrade.  See `juju debug-log` for details."
            ) from e
        self.log.info(
            f"Attempting to upgrade from control plane version {versions['control_plane']} "
            f"to client version {versions['client']}"
        )

        try:
            _validate_upgrade_version(versions)
        except ValueError as e:
            self.log.error(UPGRADE_FAILED_MSG.format(message=str(e)))
            raise GenericCharmRuntimeError(
                "Failed to upgrade.  See `juju debug-log` for details."
            ) from e

        # Use istioctl precheck to confirm the upgrade should be safe
        try:
            self._log_and_set_status(MaintenanceStatus("Executing `istioctl precheck`"))
            istioctl.precheck()
        except IstioctlError as e:
            # TODO: Expand this message.  Give user any output of the failed command
            self.log.error(UPGRADE_FAILED_MSG.format(message="`istioctl precheck` failed."))
            raise GenericCharmRuntimeError(
                "Failed to upgrade.  See `juju debug-log` for details."
            ) from e
        except Exception as e:
            self.log.error(UPGRADE_FAILED_MSG.format(message="An unknown error occurred."))
            raise GenericCharmRuntimeError(
                "Failed to upgrade.  See `juju debug-log` for details."
            ) from e

        # Execute the upgrade
        try:
            self._log_and_set_status(
                MaintenanceStatus("Executing `istioctl upgrade` for our configuration")
            )
            istioctl.upgrade()
        except IstioctlError as e:
            # TODO: Expand this message.  Give user any output of the failed command
            self.log.error(UPGRADE_FAILED_MSG.format(message="`istioctl upgrade` failed."))
            raise GenericCharmRuntimeError(
                "Failed to upgrade.  See `juju debug-log` for details."
            ) from e
        except Exception as e:
            self.log.error(UPGRADE_FAILED_MSG.format(message="An unknown error occurred."))
            raise GenericCharmRuntimeError(
                "Failed to upgrade.  See `juju debug-log` for details."
            ) from e

        # Patch any known issues with the upgrade
        client_version = Version(versions["client"])
        if Version("1.12.0") <= client_version < Version("1.15.0"):
            self._log_and_set_status(
                MaintenanceStatus(f"Fixing webhooks from upgrade to {str(client_version)}")
            )
            self._patch_istio_validating_webhook()

        self.log.info("Upgrade complete.")
        self.unit.status = ActiveStatus()

    def handle_default_gateway(self, event):
        """Handles creating gateways from charm config

        Side effect: self.handle_ingress() is also invoked by this handler as ingress
        resources depend on the default_gateway
        """
        # Clean-up resources
        self._resource_handler.delete_existing_resources(
            resource=self._resource_handler.get_custom_resource_class_from_filename(
                filename="gateway.yaml.j2"
            ),
            labels={
                f"app.{self.app.name}.io/is-workload-entity": "true",
            },
            namespace=self.model.name,
        )
        t = self.env.get_template("gateway.yaml.j2")
        gateway = self.model.config["default-gateway"]
        secret_name = (
            f"{self.app.name}-gateway-secret"
            if self.model.config["ssl-crt"] and self.model.config["ssl-key"]
            else None
        )
        manifest = None
        if secret_name:
            secret = self.env.get_template("gateway-secret.yaml.j2")
            manifest_secret = secret.render(
                secret_name=secret_name,
                ssl_crt=self.model.config["ssl-crt"],
                ssl_key=self.model.config["ssl-key"],
                model_name=self.model.name,
                app_name=self.app.name,
            )
            self._resource_handler.apply_manifest(manifest_secret)

        manifest = t.render(
            name=gateway,
            secret_name=secret_name,
            ssl_crt=self.model.config["ssl-crt"] or None,
            ssl_key=self.model.config["ssl-key"] or None,
            model_name=self.model.name,
            app_name=self.app.name,
            gateway_http_port=GATEWAY_HTTP_PORT,
            gateway_https_port=GATEWAY_HTTPS_PORT,
        )
        self._resource_handler.apply_manifest(manifest)

        # Update the ingress resources as they rely on the default_gateway
        self.handle_ingress(event)

    def handle_gateway_info_relation(self, event):
        if not self.model.relations["gateway-info"]:
            self.log.info("No gateway-info relation found")
            return
        is_gateway_created = self._resource_handler.validate_resource_exist(
            resource_type=self._resource_handler.get_custom_resource_class_from_filename(
                "gateway.yaml.j2"
            ),
            resource_name=self.model.config["default-gateway"],
            resource_namespace=self.model.name,
        )
        if is_gateway_created:
            self.gateway.send_gateway_relation_data(
                self.app, self.model.config["default-gateway"], self.model.name
            )
        else:
            self.log.info("Gateway is not created yet. Skip sending gateway relation data.")

    def send_info(self, event):
        if self.interfaces["istio-pilot"]:
            self.interfaces["istio-pilot"].send_data(
                {"service-name": f"istiod.{self.model.name}.svc", "service-port": "15012"}
            )
        else:
            self.log.debug(f"Unable to send data, deferring event: {event}")
            event.defer()

    def handle_ingress(self, event):
        # FIXME: sending the data every single time
        # is not a great design, a better one involves refactoring and
        # probably changing SDI
        self.send_info(event)

        try:
            if not self._is_gateway_service_up():
                self.log.info(
                    "No gateway address returned - this may be transitory, but "
                    "if it persists it is likely an unexpected error. "
                    "Deferring this event"
                )
                self.unit.status = WaitingStatus("Waiting for gateway address")
                event.defer()
                return
        except (ApiError, TypeError) as e:
            if isinstance(e, ApiError):
                self.log.debug(
                    "ApiError: Could not get istio-ingressgateway-workload, deferring this event"
                )
                self.unit.status = WaitingStatus(
                    "Missing istio-ingressgateway-workload service, deferring this event"
                )
                event.defer()
            elif isinstance(e, TypeError):
                self.log.debug("TypeError: No ip address found, deferring this event")
                self.unit.status = WaitingStatus("Waiting for ip address")
                event.defer()
            else:
                self.log.error("Unexpected exception.  Exception was:")
                self.log.exception(e)
            return

        ingress = self.interfaces["ingress"]

        if ingress:
            # Filter out data we sent back.
            routes = {
                (rel, app): route
                for (rel, app), route in sorted(
                    ingress.get_data().items(), key=lambda tup: tup[0][0].id
                )
                if app != self.app
            }
        else:
            routes = {}

        if isinstance(event, (RelationBrokenEvent)):
            # The app-level data is still visible on a broken relation, but we
            # shouldn't be keeping the VirtualService for that related app.
            del routes[(event.relation, event.app)]

        t = self.env.get_template("virtual_service.yaml.j2")
        gateway = self.model.config["default-gateway"]

        self.unit.status = ActiveStatus()

        def get_kwargs(route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {"gateway": gateway, "app_name": self.app.name, **route}

            if "namespace" not in kwargs:
                kwargs["namespace"] = self.model.name

            return kwargs

        # TODO: we could probably extract the rendering bits from the charm code
        virtual_services = "\n---".join(
            t.render(**get_kwargs(route)).strip().strip("---")
            for ((_, app), route) in routes.items()
        )

        self._resource_handler.reconcile_desired_resources(
            resource=self._resource_handler.get_custom_resource_class_from_filename(
                filename="virtual_service.yaml.j2"
            ),
            namespace=self.model.name,
            desired_resources=virtual_services,
        )

    def handle_ingress_auth(self, event):
        auth_routes = self.interfaces["ingress-auth"]
        if auth_routes:
            auth_routes = list(auth_routes.get_data().values())
        else:
            auth_routes = []

        if not auth_routes:
            self.log.info("Skipping auth route creation due to empty list")
            return

        if not all(ar.get("service") for ar in auth_routes):
            self.model.unit.status = WaitingStatus(
                "Waiting for auth route connection information."
            )
            return

        if self.model.config["ssl-crt"] and self.model.config["ssl-key"]:
            gateway_port = GATEWAY_HTTPS_PORT
        else:
            gateway_port = GATEWAY_HTTP_PORT

        t = self.env.get_template("auth_filter.yaml.j2")
        auth_filters = "".join(
            t.render(
                namespace=self.model.name,
                app_name=self.app.name,
                gateway_port=gateway_port,
                **{
                    "request_headers": yaml.safe_dump(
                        [{"exact": h} for h in r.get("allowed-request-headers", [])],
                        default_flow_style=True,
                    ),
                    "response_headers": yaml.safe_dump(
                        [{"exact": h} for h in r.get("allowed-response-headers", [])],
                        default_flow_style=True,
                    ),
                    "port": r["port"],
                    "service": r["service"],
                },
            )
            for r in auth_routes
        )

        self._resource_handler.delete_existing_resources(
            self._resource_handler.get_custom_resource_class_from_filename(
                filename="auth_filter.yaml.j2"
            ),
            namespace=self.model.name,
        )
        self._resource_handler.apply_manifest(auth_filters, namespace=self.model.name)

    @property
    def _istiod_svc(self):
        try:
            exporter_service = self.lightkube_client.get(
                res=Service, name="istiod", namespace=self.model.name
            )
            exporter_ip = exporter_service.spec.clusterIP
        except ApiError as e:
            if e.status.code == 404:
                return None
            raise
        else:
            return exporter_ip

    def _is_gateway_service_up(self):
        """Returns True if the ingress gateway service is up, else False."""
        svc = self._get_gateway_service()

        if svc.spec.type == "NodePort":
            # TODO: do we need to interrogate this further for status?
            return True
        if _get_gateway_address_from_svc(svc) is not None:
            return True
        return False

    def _get_gateway_service(self):
        """Returns a lightkube Service object for the gateway service."""
        # FIXME: service name is hardcoded and depends on the istio gateway application name being
        #  `istio-ingressgateway`.  This is very fragile
        # TODO: extract this from charm code
        # TODO: What happens if this service does not exist?  We should check on that and then add
        #  tests to confirm this works
        svc = self.lightkube_client.get(
            Service, name=GATEWAY_WORKLOAD_SERVICE_NAME, namespace=self.model.name
        )
        return svc

    def _log_and_set_status(self, status):
        """Sets the status of the charm and logs the status message.

        TODO: Move this to Chisme

        Args:
            status: The status to set
        """
        self.unit.status = status

        # For some reason during unit tests, self.log is not available.  Workaround this for now
        logger = logging.getLogger(__name__)

        log_destination_map = {
            ActiveStatus: logger.info,
            BlockedStatus: logger.warning,
            MaintenanceStatus: logger.info,
            WaitingStatus: logger.info,
        }

        log_destination_map[type(status)](status.message)

    def _patch_istio_validating_webhook(self):
        """Patch ValidatingWebhookConfiguration from istioctl v1.12-v1.14 to use correct namespace.

        istioctl v1.12, v1.13, and v1.14 have a bug where the ValidatingWebhookConfiguration
        istiod-default-validator looks for istiod in the `istio-system` namespace rather than the
        namespace actually used for istio.  This function patches this webhook configuration to
        use the correct namespace.

        If the webhook configuration does not exist or is already correct, this has no effect.
        """
        self.log.info(
            "Attempting to patch istiod-default-validator webhook to ensure it points to"
            " correct namespace."
        )
        try:
            vwc = self.lightkube_client.get(
                ValidatingWebhookConfiguration, name="istiod-default-validator"
            )
        except ApiError as e:
            # If the webhook doesn't exist, we don't need to patch it
            self.log.info("No istiod-default-validator webhook found - skipping patch operation.")
            if e.status.code == 404:
                return
            raise e

        vwc.metadata.managedFields = None
        vwc.webhooks[0].clientConfig.service.namespace = self.model.name
        self.lightkube_client.patch(
            ValidatingWebhookConfiguration,
            "istiod-default-validator",
            vwc,
            field_manager=self.app.name,
            force=True,
        )
        self.log.info("istiod-default-validator webhook successfully patched")


def _get_gateway_address_from_svc(svc):
    """Returns the gateway service address from a kubernetes Service.

    If the gateway isn't available or doesn't have a load balancer address yet,
    returns None.


    Args:
        svc: The lightkube Service object to interrogate

    Returns:
        (str): The hostname or IP address of the gateway service (or None)
    """
    # return gateway address: hostname or IP; None if not set
    gateway_address = None

    if svc.spec.type == "ClusterIP":
        gateway_address = svc.spec.clusterIP
    elif svc.spec.type == "LoadBalancer":
        gateway_address = _get_address_from_loadbalancer(svc)

    return gateway_address


def _get_address_from_loadbalancer(svc):
    """Returns a hostname or IP address from a LoadBalancer service.

    Args:
        svc: The lightkube Service object to interrogate

    Returns:
          (str): The hostname or IP address of the LoadBalancer service
    """
    ingresses = svc.status.loadBalancer.ingress
    if len(ingresses) != 1:
        if len(ingresses) == 0:
            return None
        else:
            raise ValueError("Unknown situation - LoadBalancer service has more than one ingress")

    ingress = svc.status.loadBalancer.ingress[0]
    if getattr(ingress, "hostname", None) is not None:
        return svc.status.loadBalancer.ingress[0].hostname
    elif getattr(ingress, "ip", None) is not None:
        return svc.status.loadBalancer.ingress[0].ip
    else:
        raise ValueError("Unknown situation - LoadBalancer service has no hostname or IP")


def _validate_upgrade_version(versions) -> bool:
    """Validates that the version of istioctl can upgrade the currently deployed Istio.

    This asserts that the istioctl version is equal to or at most greater than the current Istio
    control plane by no more than one minor version.

    Args:
        versions (dict): A dictionary containing:
                            client: the client version (eg: istioctl)
                            control_plane: the control plane version (eg: istiod)

    Returns True if this is the case, else raises an exception with details.
    """
    client_version = Version(versions["client"])
    control_plane_version = Version(versions["control_plane"])

    if client_version < control_plane_version:
        raise ValueError(
            "Client version is older than control plane version.  "
            "This is not supported by this charm."
        )
    elif client_version.minor - control_plane_version.minor > 1:
        raise ValueError(
            "Client version is more than one minor version ahead of control plane version.  "
            "This is not supported by this charm."
        )
    elif client_version.major != control_plane_version.major:
        raise ValueError(
            "Client version is a different major version to control plane version.  "
            "This is not supported by this charm."
        )

    return True


if __name__ == "__main__":
    main(Operator)
