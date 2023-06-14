#!/usr/bin/env python3

import logging
import subprocess
from typing import List, Optional

import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import (
    KubernetesResourceHandler,
    create_charm_default_labels,
)
from charmed_kubeflow_chisme.status_handling import get_first_worst_error
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.istio_pilot.v0.istio_gateway_info import (
    DEFAULT_RELATION_NAME as GATEWAY_INFO_RELATION_NAME,
)
from charms.istio_pilot.v0.istio_gateway_info import GatewayProvider
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.admissionregistration_v1 import ValidatingWebhookConfiguration
from lightkube.resources.core_v1 import Secret, Service
from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, Application, BlockedStatus, MaintenanceStatus, WaitingStatus
from packaging.version import Version
from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    get_interface,
    get_interfaces,
)

from istioctl import Istioctl, IstioctlError

ENVOYFILTER_LIGHTKUBE_RESOURCE = create_namespaced_resource(
    group="networking.istio.io", version="v1alpha3", kind="EnvoyFilter", plural="envoyfilters"
)
GATEWAY_LIGHTKUBE_RESOURCE = create_namespaced_resource(
    group="networking.istio.io", version="v1beta1", kind="Gateway", plural="gateways"
)
VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE = create_namespaced_resource(
    group="networking.istio.io",
    version="v1alpha3",
    kind="VirtualService",
    plural="virtualservices",
)

GATEWAY_PORTS = {
    "http": 8080,
    "https": 8443,
}
GATEWAY_TEMPLATE_FILES = ["src/manifests/gateway.yaml.j2"]
KRH_GATEWAY_SCOPE = "gateway"
METRICS_PORT = 15014
INGRESS_AUTH_RELATION_NAME = "ingress-auth"
INGRESS_AUTH_TEMPLATE_FILES = ["src/manifests/auth_filter.yaml.j2"]
INGRESS_RELATION_NAME = "ingress"
ISTIO_PILOT_RELATION_NAME = "istio-pilot"
KRH_INGRESS_SCOPE = "ingress"
VIRTUAL_SERVICE_TEMPLATE_FILES = ["src/manifests/virtual_service.yaml.j2"]
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
        self._field_manager = "lightkube"

        # Event handling for managing the Istio control plane
        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)
        self.framework.observe(self.on.upgrade_charm, self.upgrade_charm)

        # Event handling for managing our Istio resources
        # Configuration changes always result in reconciliation
        # This captures any changes to the default-gateway's config
        self.framework.observe(self.on.config_changed, self.reconcile)

        # Watch:
        # * relation_created: because we send data to the other side whenever we see a related app
        # * relation_joined: because we send data to the other side whenever we see a related unit
        self.framework.observe(
            self.on[GATEWAY_INFO_RELATION_NAME].relation_created, self.reconcile
        )
        self.framework.observe(self.on[GATEWAY_INFO_RELATION_NAME].relation_joined, self.reconcile)
        # Configure the gateway-info provider
        self.gateway_provider = GatewayProvider(self)

        # Watch:
        # * relation_created: because we send data to the other side whenever we see a related app
        # * relation_joined: because we send data to the other side whenever we see a related unit
        # * relation_changed: because of SDI's data versioning model, which first agrees on the
        #                     schema version and then sends the rest of the data
        self.framework.observe(self.on["istio-pilot"].relation_created, self.reconcile)
        self.framework.observe(self.on["istio-pilot"].relation_joined, self.reconcile)
        self.framework.observe(self.on["istio-pilot"].relation_changed, self.reconcile)

        # Watch:
        # * relation_changed: because if the remote data updates, we need to update our resources
        # * relation_broken: because this is an application-level data exchange, so if the related
        #   application goes away we need to remove their resources
        self.framework.observe(self.on["ingress"].relation_changed, self.reconcile)
        self.framework.observe(self.on["ingress"].relation_broken, self.reconcile)

        # Watch:
        # * relation_created: because incomplete relation data should cause removal of the gateway
        # * relation_joined: because incomplete relation data should cause removal of the gateway
        # * relation_changed: because if the remote data updates, we need to update our resources
        # * relation_broken: because this is an application-level data exchange, so if the related
        #   application goes away we need to remove their resources
        # This charm acts on relation_created/relation_joined to prevent accidentally leaving the
        # ingress unsecured.  If anything is related to ingress-auth, that indicates the user wants
        # block unauthenticated traffic through the Ingress, even if the related application has
        # not yet sent us the authentication details.  By monitoring
        # relation_created/relation_joined, we can block traffic through the Gateway until the auth
        # is set up. This prevents a broken application on the ingress-auth relation from resulting
        # in an unsecured ingress.
        self.framework.observe(self.on["ingress-auth"].relation_created, self.reconcile)
        self.framework.observe(self.on["ingress-auth"].relation_joined, self.reconcile)
        self.framework.observe(self.on["ingress-auth"].relation_changed, self.reconcile)
        self.framework.observe(self.on["ingress-auth"].relation_broken, self.reconcile)

        # Configure Observability
        self._scraping = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[
                {"static_configs": [{"targets": [f"istiod.{self.model.name}.svc:{METRICS_PORT}"]}]}
            ],
        )
        self.grafana_dashboards = GrafanaDashboardProvider(self, relation_name="grafana-dashboard")

    def install(self, _):
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

        This is the main entrypoint for the charm.  It:
        * Checks if we are the leader, exiting early with WaitingStatus if we are not
        * Sends data to the istio-pilot relation
        * Reconciles the ingress-auth relation, establishing whether we need authentication on our
          ingress gateway
        * Sets up or removes a gateway based on the ingress-auth relation
        * Sends data to the gateway-info relation
        * Reconciles the ingress relation, setting up or removing VirtualServices as required

        Throughout the above steps, any non-fatal error is caught and logged at the end of
        processing.  The Charm status will be set to the worst error type, and all errors will
        be reported in logs.
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

        # Send istiod information to the istio-pilot relation
        try:
            self._handle_istio_pilot_relation()
        except ErrorWithStatus as err:
            handled_errors.append(err)

        # Reconcile the ingress_auth relation, indicating whether we failed so we can later remove
        # the Gateway if necessary
        ingress_auth_reconcile_successful = False
        try:
            ingress_auth_data = self._get_ingress_auth_data(event)
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
                self.log.info(
                    "Removing gateway due to errors in processing the ingress-auth relation."
                )
                self._remove_gateway()
        except ErrorWithStatus as err:
            handled_errors.append(err)

        try:
            self._send_gateway_info()
        except ErrorWithStatus as err:
            handled_errors.append(err)

        try:
            # If any one of the ingress relations has a version error, this will fail fast and not
            # provide any data for us to work on (blocking reconcile on all ingress relations).
            # This is a limitation of SDI.
            ingress_data = self._get_ingress_data(event)
            self._reconcile_ingress(ingress_data)
        except ErrorWithStatus as err:
            handled_errors.append(err)

        # Reports (status and logs) any handled errors, or sets to ActiveStatus
        self._report_handled_errors(errors=handled_errors)

    def remove(self, _):
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

    def upgrade_charm(self, _):
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
        if self._use_https():
            return GATEWAY_PORTS["https"]
        else:
            return GATEWAY_PORTS["http"]

    def _get_interfaces(self):
        """Retrieve interface object."""
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise ErrorWithStatus(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(err, BlockedStatus)
        return interfaces

    def _get_ingress_auth_data(self, event) -> dict:
        """Retrieve the ingress-auth relation data without touching other interface data.

        This is a workaround to ensure that errors in other relation data, such as an incomplete
        ingress relation, do not block us from retrieving the ingress-auth data.
        """
        # Do not process data if this is a relation-broken event for this relation
        if (
            isinstance(event, RelationBrokenEvent)
            and event.relation.name == INGRESS_AUTH_RELATION_NAME
        ):
            return {}
        try:
            ingress_auth_interface = get_interface(self, INGRESS_AUTH_RELATION_NAME)
        except NoVersionsListed as err:
            raise ErrorWithStatus(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(err, BlockedStatus)

        # Filter out data we sent out.
        if ingress_auth_interface:
            # TODO: This can probably be simplified.
            ingress_auth_data = {
                (rel, app): route
                for (rel, app), route in sorted(
                    ingress_auth_interface.get_data().items(), key=lambda tup: tup[0][0].id
                )
                if app != self.app
            }

            # We only support a single ingress-auth relation, so we can unpack and return just the
            # contents
            if len(ingress_auth_data) > 1:
                raise ErrorWithStatus(
                    "Multiple ingress-auth relations are not supported.  Remove all related apps"
                    " and re-relate",
                    BlockedStatus,
                )
            ingress_auth_data = list(ingress_auth_data.values())[0]

        else:
            # If there is no ingress-auth relation, we have no data here
            ingress_auth_data = {}

        return ingress_auth_data

    def _get_ingress_data(self, event) -> dict:
        """Retrieve the ingress relation data without touching other interface data.

        This is a workaround to ensure that errors in other relation data, such as an incomplete
        ingress-auth relation, do not block us from retrieving the ingress data.
        """
        try:
            ingress_interface = get_interface(self, INGRESS_RELATION_NAME)
        except NoVersionsListed as err:
            raise ErrorWithStatus(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(err, BlockedStatus)

        # Get all route data from the ingress interface
        routes = get_routes_from_ingress_interface(ingress_interface, self.app)

        # The app-level data is still visible on a broken relation, but we
        # shouldn't be keeping the VirtualService for that related app.
        if isinstance(event, RelationBrokenEvent) and event.relation.name == INGRESS_RELATION_NAME:
            routes.pop((event.relation, event.app))

        return routes

    def _get_istio_pilot_interface(self) -> dict:
        """Retrieve the istio-pilot relation data without touching other interface data.

        This is a workaround to ensure that errors in other relation data, such as an incomplete
        ingress-auth relation, do not block us from retrieving the ingress data.
        """
        try:
            istio_pilot_interface = get_interface(self, ISTIO_PILOT_RELATION_NAME)
        except NoVersionsListed as err:
            raise ErrorWithStatus(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(err, BlockedStatus)
        return istio_pilot_interface

    def _get_gateway_service(self):
        """Returns a lightkube Service object for the gateway service.

        Raises an ApiError(404) if the service does not exist.

        Note: this assumes that the gateway service is deployed in the same namespace as this charm

        TODO: The service name we look for is configured via the `gateway-service-name` config, but
         it should be provided directly from the istio-gateway charm.  See:
         https://github.com/canonical/istio-operators/issues/258.
        """
        svc = self._lightkube_client.get(
            Service, name=self.model.config["gateway-service-name"], namespace=self.model.name
        )
        return svc

    def _handle_istio_pilot_relation(self):
        """Handles the istio-pilot relation, sending information about the Istio daemon."""
        istio_pilot_interface = self._get_istio_pilot_interface()
        if istio_pilot_interface:
            # If something is related, send the data.  Otherwise skip
            istio_pilot_interface.send_data(
                {"service-name": f"istiod.{self.model.name}.svc", "service-port": "15012"}
            )

    @property
    def _lightkube_client(self):
        """Returns a lightkube client configured for this charm."""
        return Client(namespace=self.model.name, field_manager=self._field_manager)

    def _send_gateway_info(self):
        """Sends gateway information to all related apps.

        Note, this should be done after any of the following:
        * a new application relates to gateway-info
        * config_changed occurs (because gateway_name may have changed)
        * any changes are made to the Gateway or ingress-auth, because that may change the status
          of the gateway
        """
        self.gateway_provider.send_gateway_relation_data(
            gateway_name=self._gateway_name,
            gateway_namespace=self._gateway_namespace,
            gateway_up=self._is_gateway_up,
        )

    def _reconcile_gateway(self):
        """Creates or updates the Gateway resources.

        If secured with TLS, this also deploys a secret with the certificate and key.
        """
        # Secure the gateway, if enabled
        use_https = self._use_https()

        if use_https:
            ssl_crt = self.model.config["ssl-crt"]
            ssl_key = self.model.config["ssl-key"]
        else:
            ssl_crt = None
            ssl_key = None

        context = {
            "gateway_name": self._gateway_name,
            "namespace": self._gateway_namespace,
            "port": self._gateway_port,
            "ssl_crt": ssl_crt,
            "ssl_key": ssl_key,
            "secure": use_https,
        }

        krh = KubernetesResourceHandler(
            field_manager=self._field_manager,
            template_files=GATEWAY_TEMPLATE_FILES,
            context=context,
            logger=self.log,
            labels=create_charm_default_labels(
                application_name=self.app.name, model_name=self.model.name, scope=KRH_GATEWAY_SCOPE
            ),
            resource_types={GATEWAY_LIGHTKUBE_RESOURCE, Secret},
        )
        krh.reconcile()

    def _reconcile_ingress(self, routes: List[dict]):
        """Reconcile all Ingress relations, managing the VirtualService resources.

        Args:
            routes: a list of ingress relation data dicts, each containing data for keys:
                service
                port
                namespace
                prefix
                rewrite
        """
        # We only need the route data, not the relation keys
        routes = list(routes.values())

        # ingress relation schema v2+ requires `namespace`, but v1 did not have `namespace`.
        # For backward compatibility support, use istio-pilot's namespace by default if omitted.
        for route in routes:
            if "namespace" not in route:
                route["namespace"] = self.model.name

        context = {
            "charm_namespace": self.model.name,
            "gateway_name": self._gateway_name,
            "gateway_namespace": self._gateway_namespace,
            "routes": routes,
        }

        krh = KubernetesResourceHandler(
            field_manager=self._field_manager,
            template_files=VIRTUAL_SERVICE_TEMPLATE_FILES,
            context=context,
            logger=self.log,
            labels=create_charm_default_labels(
                application_name=self.app.name, model_name=self.model.name, scope=KRH_INGRESS_SCOPE
            ),
            resource_types={VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE},
        )

        krh.reconcile()

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
            _remove_envoyfilter(name=envoyfilter_name, namespace=self.model.name)
            return

        context = {
            "auth_service_name": ingress_auth_data["service"],
            "auth_service_namespace": self.model.name,  # Assumed to be in the same namespace
            "app_name": self.app.name,
            "envoyfilter_name": envoyfilter_name,
            "envoyfilter_namespace": self.model.name,
            "gateway_ports": list(GATEWAY_PORTS.values()),
            "port": ingress_auth_data["port"],
            "request_headers": ingress_auth_data["allowed-request-headers"],
            "response_headers": ingress_auth_data["allowed-response-headers"],
        }

        krh = KubernetesResourceHandler(
            field_manager=self._field_manager,
            template_files=INGRESS_AUTH_TEMPLATE_FILES,
            context=context,
            logger=self.log,
        )

        krh.apply()

    def _remove_gateway(self):
        """Remove any deployed Gateway resources."""
        krh = KubernetesResourceHandler(
            field_manager=self._field_manager,
            logger=self.log,
            labels=create_charm_default_labels(
                application_name=self.app.name, model_name=self.model.name, scope=KRH_GATEWAY_SCOPE
            ),
            resource_types={GATEWAY_LIGHTKUBE_RESOURCE, Secret},
        )
        krh.delete()

    def _report_handled_errors(self, errors):
        """Sets status to the worst error's status and logs all messages, otherwise sets Active."""
        if errors:
            worst_error = get_first_worst_error(errors)
            status_to_publish = worst_error.status_type(
                f"Execution handled {len(errors)} errors.  See logs for details."
            )
            self._log_and_set_status(status_to_publish)
            for i, error in enumerate(errors):
                self.log.info(f"Handled error {i}/{len(errors)}: {error.status}")
        else:
            self.unit.status = ActiveStatus()

    @property
    def _gateway_name(self):
        """Returns the name of the Gateway we will create."""
        return self.model.config["default-gateway"]

    @property
    def _gateway_namespace(self):
        """Returns the namespace of the Gateway we will create, which is the same as the model."""
        return self.model.name

    @property
    def _is_gateway_object_up(self):
        """Return whether the gateway object exists."""
        try:
            self._lightkube_client.get(
                GATEWAY_LIGHTKUBE_RESOURCE,
                name=self._gateway_name,
                namespace=self._gateway_namespace,
            )
            return True
        except ApiError as e:
            # If the object is missing, return False.  For other errors, raise.
            if e.status.code == 404:
                return False
            raise

    @property
    def _is_gateway_service_up(self):
        """Returns True if the ingress gateway service is up, else False.

        TODO: This should be something provided via a relation to istio-gateway, which would know
         this info better.
        """
        try:
            svc = self._get_gateway_service()
        except ApiError as e:
            # If we cannot find the gateway service, the service is not up
            if e.status.code == 404:
                return False

        if svc.spec.type == "NodePort":
            # TODO: do we need to interrogate this further for status?
            return True
        if _get_gateway_address_from_svc(svc) is not None:
            return True

        # The service is configured in an unknown way, or it does not have an IP address
        return False

    @property
    def _is_gateway_up(self):
        """Returns True if the Gateway object and its Service are both up."""
        return self._is_gateway_service_up and self._is_gateway_object_up

    @property
    def _istiod_svc(self):
        try:
            exporter_service = self._lightkube_client.get(
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

        log_destination_map = {
            ActiveStatus: self.log.info,
            BlockedStatus: self.log.warning,
            MaintenanceStatus: self.log.info,
            WaitingStatus: self.log.info,
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
        lightkube_client = self._lightkube_client
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

    def _use_https(self):
        if _xor(self.model.config["ssl-crt"], self.model.config["ssl-key"]):
            # Fail if ssl is only partly configured as this is probably a mistake
            raise ErrorWithStatus(
                "Charm config for ssl-crt and ssl-key must either both be set or unset",
                BlockedStatus,
            )
        if self.model.config["ssl-crt"] and self.model.config["ssl-key"]:
            return True
        else:
            return False


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


def _remove_envoyfilter(name: str, namespace: str, logger: Optional[logging.Logger] = None):
    """Remove an EnvoyFilter resource, ignoring if the resource is already removed.

    Args:
        name: The name of the EnvoyFilter resource to remove
        namespace: The namespace of the EnvoyFilter resource to remove
        logger: (optional) logger to log any messages to
    """
    lightkube_client = Client()
    try:
        lightkube_client.delete(ENVOYFILTER_LIGHTKUBE_RESOURCE, name=name, namespace=namespace)
    except ApiError as e:
        if logger:
            logger.info(
                f"Failed to remove EnvoyFilter {name} in namespace {namespace} -"
                f" resource does not exist.  It may have been removed already."
            )
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


def get_routes_from_ingress_interface(
    ingress_interface: Optional[dict], this_app: Application
) -> dict:
    """Returns a dict of route data from the ingress interface."""
    routes = {}

    if ingress_interface:
        sorted_interface_data = sorted(
            ingress_interface.get_data().items(), key=lambda tup: tup[0][0].id
        )

        for (rel, app), route in sorted_interface_data:
            if app != this_app:
                routes[(rel, app)] = route
    else:
        # If there is no ingress-auth relation, we have no data here
        pass

    return routes


def _xor(a, b):
    """Returns True if exactly one of a and b is True, else False."""
    if (a and not b) or (b and not a):
        return True
    else:
        return False


if __name__ == "__main__":
    main(Operator)
