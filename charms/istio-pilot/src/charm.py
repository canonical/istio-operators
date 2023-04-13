#!/usr/bin/env python3

import logging
import subprocess

import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.admissionregistration_v1 import ValidatingWebhookConfiguration
from lightkube.resources.core_v1 import Service
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from packaging.version import Version
from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    get_interface,
    get_interfaces,
)

from istio_gateway_info_provider import RELATION_NAME as GATEWAY_INFO_RELATION_NAME
from istio_gateway_info_provider import GatewayProvider
from istioctl import Istioctl, IstioctlError

GATEWAY_HTTP_PORT = 8080
GATEWAY_HTTPS_PORT = 8443
METRICS_PORT = 15014
INGRESS_AUTH_RELATION_NAME = "ingress-auth"
INGRESS_AUTH_TEMPLATE_FILES = ["manifests/auth_filter.yaml.j2"]
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

# Helper to retry calling a function over 15 minutes
RETRY_FOR_15_MINUTES = tenacity.Retrying(
    stop=tenacity.stop_after_delay(60 * 15),
    wait=tenacity.wait_fixed(2),
    reraise=True,
)


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.log = logging.getLogger(__name__)

        # TODO: Refactor this?  Putting it in init means we have to always mock it in unit tests
        # self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")

        # TODO: Update resource_handler to use the newer handler
        # # Configure resource handler
        # self.env = Environment(loader=FileSystemLoader("src"))
        # self._resource_files = [
        #     "gateway.yaml.j2",
        #     "auth_filter.yaml.j2",
        #     "virtual_service.yaml.j2",
        # ]
        self._resource_handler = None

        # Event handling for managing the Istio control plane
        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)
        self.framework.observe(self.on.upgrade_charm, self.upgrade_charm)

        # Event handling for managing our Istio resources
        # Configuration changes always result in reconciliation
        # This captures any changes to the default-gateway's config
        self.framework.observe(self.on.config_changed, self.reconcile)

        # Watch:
        # * relation_joined: because we send data to the other side whenever we see a related app
        self.framework.observe(
            self.on[GATEWAY_INFO_RELATION_NAME].relation_created, self.reconcile
        )

        # Watch:
        # * relation_joined: because we send data to the other side whenever we see a related app
        # * relation_changed: because of SDI's data versioning model, which first agrees on the
        #                     schema version and then sends the rest of the data
        # TODO: * relation_broken: is this needed?
        self.framework.observe(self.on["istio-pilot"].relation_created, self.reconcile)
        self.framework.observe(self.on["istio-pilot"].relation_changed, self.reconcile)
        self.framework.observe(self.on["istio-pilot"].relation_broken, self.reconcile)

        # Watch:
        # * relation_changed: because if the remote data updates, we need to update our resources
        # * relation_broken: because this is an application-level data exchange, so if the related
        #   application goes away we need to remove their resources
        self.framework.observe(self.on["ingress"].relation_changed, self.reconcile)
        self.framework.observe(self.on["ingress"].relation_broken, self.reconcile)
        self.framework.observe(self.on["ingress-auth"].relation_changed, self.reconcile)
        self.framework.observe(self.on["ingress-auth"].relation_broken, self.reconcile)

        # Configure Observability
        # TODO: Re-add this, but is there a way to do it without having to mock it in unit tests?
        # if self._istiod_svc:
        #     self._scraping = MetricsEndpointProvider(
        #         self,
        #         relation_name="metrics-endpoint",
        #         jobs=[{"static_configs": [{"targets": [f"{self._istiod_svc}:{METRICS_PORT}"]}]}],
        #     )
        # self.grafana_dashboards = GrafanaDashboardProvider(
        #       self, relation_name="grafana-dashboard"
        # )

        # Configure the gateway-info provider
        # TODO: Rename this to gateway_info?
        # TODO: Can the gateway-info provider event handling just be moved to this class, and main
        #  doesn't need to know about it (similar to obs libs)?  We'd probably want it to be after
        #  the main handlers.
        #  If we break this into a separate handler, it will need to trigger on anything that
        #  triggers a reconcile because the Gateway's status could change during those events
        self.gateway = GatewayProvider(self)
        # Configure Observability
        # TODO: Re-add this, but is there a way to do it without having to mock it in unit tests?
        # if self._istiod_svc:
        #     self._scraping = MetricsEndpointProvider(
        #         self,
        #         relation_name="metrics-endpoint",
        #         jobs=[{"static_configs": [{"targets": [f"{self._istiod_svc}:{METRICS_PORT}"]}]}],
        #     )
        # self.grafana_dashboards = GrafanaDashboardProvider(
        #     self,
        #     relation_name="grafana-dashboard"
        # )

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

    def reconcile(self, event):
        """Reconcile the state of the charm.

        TODO: Add more details
        """
        # If we are not the leader, the charm should do nothing else
        try:
            self._check_leader()
        except ErrorWithStatus as err:
            self._log_and_set_status(err.status)
            return

        # This charm may hit multiple, non-fatal errors during the reconciliation.  Collect them
        # so that we can report them at the end.
        handled_errors = []

        ingress_auth_reconcile_successful = False
        try:
            ingress_auth_data = self._get_ingress_auth_data()
            self._reconcile_ingress_auth(ingress_auth_data)
            ingress_auth_reconcile_successful = True
        except ErrorWithStatus as err:
            handled_errors.append(err)

        try:
            # If handling the ingress_auth relation fails, remove the Gateway to prevent
            # unauthenticated traffic
            if ingress_auth_reconcile_successful:
                self._reconcile_gateway()
            else:
                # TODO: Log here?
                self._remove_gateway()
        except ErrorWithStatus as err:
            handled_errors.append(err)

        try:
            self._send_gateway_info()
        except ErrorWithStatus as err:
            handled_errors.append(err)

        try:
            # TODO: Should I break these up so we can have more granular behaviour?
            #  I could just get the ingress interface directly.  Although I can't break that
            #  up into its components easily due to how SDI is written.
            # If any relation in this group has a version error, this will fail fast and not
            # provide any data for us to work on.  This is a limitation of SDI.
            interfaces = self._get_interfaces()
            self._reconcile_ingress(interfaces["ingress"], event)
        except ErrorWithStatus as err:
            # One or more related applications resulted in an error
            handled_errors.append(err)
            self._log_and_set_status(err.status)

        # TODO: If we have and handled_errors, report them
        self._report_handled_errors(handled_errors)

        raise NotImplementedError("this is just a pseudo-code example")

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

        # TODO: Update resource_handler to use the newer handler
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

        # Wait for the upgrade to complete before progressing
        self._log_and_set_status(
            MaintenanceStatus("Waiting for Istio upgrade to roll out in cluster")
        )
        _wait_for_update_rollout(istioctl, RETRY_FOR_15_MINUTES, self.log)

        # Patch any known issues with the upgrade
        client_version = Version(versions["client"])
        if Version("1.12.0") <= client_version < Version("1.15.0"):
            self._log_and_set_status(
                MaintenanceStatus(f"Fixing webhooks from upgrade to {str(client_version)}")
            )
            self._patch_istio_validating_webhook()

        self.log.info("Upgrade complete.")
        self.unit.status = ActiveStatus()

    def _check_leader(self):
        """Check if this unit is a leader."""
        if not self.unit.is_leader():
            self.log.info("Not a leader, skipping setup")
            raise ErrorWithStatus("Waiting for leadership", WaitingStatus)

    @property
    def _gateway_port(self):
        if _xor(self.model.config["ssl-crt"], self.model.config["ssl-key"]):
            # Fail if ssl is only partly configured as this is probably a mistake
            raise ErrorWithStatus("Charm config for ssl-crt and ssl-key must either both be set or unset", BlockedStatus)

        if self.model.config["ssl-crt"] and self.model.config["ssl-key"]:
            return GATEWAY_HTTPS_PORT
        else:
            return GATEWAY_HTTP_PORT

    def _get_interfaces(self):
        """Retrieve interface object."""
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise ErrorWithStatus(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(err, BlockedStatus)
        return interfaces

    def _get_ingress_auth_data(self) -> dict:
        """Retrieve the ingress-auth relation data without touching other interface data.

        This is a workaround to ensure that errors in other relation data, such as an incomplete
        ingress relation, do not block us from retrieving the ingress-auth data.
        """
        try:
            ingress_auth_interface = get_interface(self, INGRESS_AUTH_RELATION_NAME)
        except NoVersionsListed as err:
            raise ErrorWithStatus(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(err, BlockedStatus)

        # Filter out data we sent out.
        if ingress_auth_interface:
            ingress_auth_data = {
                (rel, app): route
                for (rel, app), route in sorted(
                    ingress_auth_interface.get_data().items(), key=lambda tup: tup[0][0].id
                )
                if app != self.app
            }
        else:
            # If there is no ingress-auth relation, we have no data here
            ingress_auth_data = {}

        if len(ingress_auth_data) > 1:
            raise ErrorWithStatus(
                "Multiple ingress-auth relations are not supported", BlockedStatus
            )

        return ingress_auth_data

    def _get_gateway_service(self):
        """Returns a lightkube Service object for the gateway service."""
        # FIXME: service name is configured via config, but it should really be provided directly
        #  from the istio-gateway.  Providing here as a config at least makes this less rigid than
        #  assuming the name.
        # TODO: What happens if this service does not exist?  We should check on that and then add
        #  tests to confirm this works

        # Note: this assumes that the gateway service is deployed in the same namespace as this
        # charm
        svc = self.lightkube_client.get(
            Service, name=self.model.config["gateway-service-name"], namespace=self.model.name
        )
        return svc

    def _send_gateway_info(self):
        """Sends gateway information to all related apps."""
        # TODO: Can any of this be put into the lib?
        # TODO: Could this be a lib class that subscribes its own event handlers?  It needs to run
        #  after the main event handlers - is the order guaranteed?

        # Maybe this always send data, but have an "is this up" field in the relation as well that
        # captures the is_gateway_ready() part?

        # Send the Gateway information if the Gateway is created, or send a null response if it is
        # not up
        # Should we also log something here about what we're sending?
        # if self.is_gateway_ready():
        #   self.gateway.send_gateway_relation_data(self.app, self.model.config["default-gateway"])
        # else:
        #   self.gateway.send_gateway_relation_data(self.app, "")  # ???

        raise NotImplementedError()

    def _reconcile_gateway(self):
        """Reconcile the Gateway resource."""
        # TODO: put any gateway removal logic in _remove_gateway(), that way it is easy to call
        #  when ingress-auth fails.
        raise NotImplementedError()

    def _reconcile_ingress(self, ingress_interface):
        """Reconcile all Ingress relations, managing the VirtualService resources."""
        # TODO: Make sure you delete any that are no longer relevant, without deleting everyone
        #  else's
        raise NotImplementedError()

    def _reconcile_ingress_auth(self, ingress_auth_data: dict):
        """Reconcile the EnvoyFilter which is controlled by the ingress-auth relation data.

        If ingress_auth_data is an empty dict, this results in any existing ingress-auth
        EnvoyFilter previously deployed here to be deleted.

        Limitations:
            * this function supports only ingress_auth_data with a single entry.  If we support
              multiple entries, this needs refactoring
            * the auth_filter yaml template has a hard-coded workloadSelector for the Gateway
        """
        envoyfilter_name = f"{self.app.name}-authn-filter"

        if len(ingress_auth_data) == 0:
            self.log.info("No ingress-auth data found - deleting any existing EnvoyFilter")
            _remove_envoyfilter(envoyfilter_name)
            return

        context = {
            "auth_service_name": ingress_auth_data['service'],
            "auth_service_namespace": self.model.name,  # Assumed to be in the same namespace
            "app_name": self.app.name,
            "envoyfilter_name": envoyfilter_name,
            "gateway_port": self._gateway_port,
            "port": ingress_auth_data['port'],
            "request_headers": ingress_auth_data['request_headers'],
            "response_headers": ingress_auth_data['response_headers'],
        }

        krh = KubernetesResourceHandler(
            field_manager=self.app.name,
            template_files=INGRESS_AUTH_TEMPLATE_FILES,
            context=context,
            logger=self.log
        )

        krh.apply()

    def _remove_gateway(self):
        """Remove the Gateway resource."""
        raise NotImplementedError()

    def _report_handled_errors(self, errors):
        """Sets status to the worst error's status and logs all messages, otherwise sets Active.

        TODO: expand this
        """
        # TODO: Set Active otherwise?  Call a "check my status" function if we have no errors?
        raise NotImplementedError()

    @property
    def _is_gateway_service_up(self):
        """Returns True if the ingress gateway service is up, else False."""
        # TODO: This should really be something provided via a relation to istio-gateway, where it
        #  tells us if things are working.
        svc = self._get_gateway_service()

        if svc.spec.type == "NodePort":
            # TODO: do we need to interrogate this further for status?
            return True
        if _get_gateway_address_from_svc(svc) is not None:
            return True
        return False

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
        lightkube_client = Client()
        try:
            vwc = lightkube_client.get(
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
        lightkube_client.patch(
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


def _remove_envoyfilter(name: str, namespace: str):
    """Remove an EnvoyFilter resource.

    Args:
        name: The name of the EnvoyFilter resource to remove
    """
    envoyfilter_resource = create_namespaced_resource(
        group="networking.istio.io", version="v1alpha3", kind="EnvoyFilter", plural="envoyfilters"
    )
    lightkube_client = Client()
    try:
        lightkube_client.delete(envoyfilter_resource, name=name, namespace=namespace)
    except ApiError as e:
        if e.status.code == 404:
            return
        raise e


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


def _wait_for_update_rollout(
    istioctl: Istioctl, retry_strategy: tenacity.Retrying, logger: logging.Logger
):
    for attempt in retry_strategy:
        # When istioctl shows the control plane version matches the client version, continue
        with attempt:
            versions = istioctl.version()
            if versions["control_plane"] != versions["client"]:
                logger.info(
                    f"Found control plane version {versions['control_plane']} - waiting for "
                    f"control plane to be version {versions['client']}."
                )
                logger.error(
                    UPGRADE_FAILED_MSG.format(
                        message="upgrade-charm handler timed out while waiting for new Istio"
                        " version to roll out."
                    )
                )
                raise GenericCharmRuntimeError(
                    "Failed to upgrade.  See `juju debug-log` for details."
                )
            else:
                logger.info(
                    f"Found control plane version ({versions['control_plane']}) matching client"
                    f" version - upgrade rollout complete"
                )
    return versions


def _xor(a, b):
    """Returns True if exactly one of a and b is True, else False."""
    if (a and not b) or (b and not a):
        return True
    else:
        return False


if __name__ == "__main__":
    main(Operator)
