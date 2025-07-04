#!/usr/bin/env python3

import base64
import logging
from typing import Dict, List, Optional

import tenacity
import yaml
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
from charms.observability_libs.v1.cert_handler import CertHandler
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Secret, Service
from ops import main
from ops.charm import CharmBase, RelationBrokenEvent
from ops.model import (
    ActiveStatus,
    Application,
    BlockedStatus,
    MaintenanceStatus,
    ModelError,
    SecretNotFoundError,
    WaitingStatus,
)
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
    group="networking.istio.io", version="v1alpha3", kind="Gateway", plural="gateways"
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
IMAGE_CONFIGURATION = "image-configuration"
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
INSTALL_FAILED_MSG = (
    "Failed to install Istio Control Plane. "
    "{message} Make sure the cluster has no Istio installations already present and "
    "that you have provided the right configuration values."
)
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

        # Instantiate a CertHandler
        self.peer_relation_name = "peers"
        self._cert_handler = CertHandler(
            self,
            key="istio-cert",
            cert_subject=self._cert_subject,
            # If _cert_subject is None, CertHandler will use the Service FQDN
            sans=[self._cert_subject] if self._cert_subject else None,
        )

        # Observe this custom event emitted by the cert_handler library on certificate
        # available, revoked, invalidated, or if the certs relation is broken
        self.framework.observe(self._cert_handler.on.cert_changed, self.reconcile)

        # Save TLS information and reconcile
        self._tls_secret_id = self.config.get("tls-secret-id")
        self.framework.observe(self.on.secret_changed, self.reconcile)

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

    def _get_image_config(self):
        """Retrieve and return image configuration."""
        image_config = yaml.safe_load(self.model.config[IMAGE_CONFIGURATION])
        return image_config

    @property
    def _istioctl_extra_flags(self):
        """Return extra flags to pass to istioctl commands."""
        image_config = self._get_image_config()
        pilot_image = image_config["pilot-image"]
        global_tag = image_config["global-tag"]
        global_hub = image_config["global-hub"]
        global_proxy_image = image_config["global-proxy-image"]
        global_proxy_init_image = image_config["global-proxy-init-image"]

        # Extra flags to pass to the istioctl install command
        # These flags will configure the container images used by the control plane
        # As well as set the access log files for help during debugging
        extra_flags = [
            "--set",
            f"values.pilot.image={pilot_image}",
            "--set",
            f"values.global.tag={global_tag}",
            "--set",
            f"values.global.hub={global_hub}",
            "--set",
            f"values.global.proxy.image={global_proxy_image}",
            "--set",
            f"values.global.proxy_init.image={global_proxy_init_image}",
            "--set",
            "meshConfig.accessLogFile=/dev/stdout",
        ]

        # The following are a set of flags that configure the CNI behaviour
        # * components.cni.enabled enables the CNI plugin
        # * values.cni.cniBinDir and values.cni.cniConfDir tell the plugin where to find
        #   the CNI binaries and config files
        # * values.sidecarInjectorWebhook.injectedAnnotations allows users to inject any
        #   annotations to the sidecar injected Pods. This particular annotation helps
        #   provide a solution for canonical/istio-operators#356
        if self._check_cni_configurations():
            extra_flags.extend(
                [
                    "--set",
                    "components.cni.enabled=true",
                    "--set",
                    f"values.cni.cniBinDir={self.model.config['cni-bin-dir']}",
                    "--set",
                    f"values.cni.cniConfDir={self.model.config['cni-conf-dir']}",
                    "--set",
                    "values.sidecarInjectorWebhook.injectedAnnotations.traffic\.sidecar\.istio\.io/excludeOutboundIPRanges=0.0.0.0/0",  # noqa
                ]
            )
        return extra_flags

    def install(self, _):
        """Install charm."""

        self._log_and_set_status(
            MaintenanceStatus("Deploying Istio control plane with Istio CNI plugin.")
        )

        # Call the istioctl wrapper to install the Istio Control Plane
        istioctl = Istioctl(
            ISTIOCTL_PATH,
            self.model.name,
            ISTIOCTL_DEPOYMENT_PROFILE,
            istioctl_extra_flags=self._istioctl_extra_flags,
        )

        try:
            istioctl.install()
        except IstioctlError as e:
            self.log.error(INSTALL_FAILED_MSG.format(message=str(e)))
            raise GenericCharmRuntimeError(
                "Failed to install control plane. See juju debug-log for details."
            ) from e

        self.unit.status = ActiveStatus()

    def reconcile(self, event):
        """Reconcile the state of the charm.

        This is the main entrypoint for the method.  It:
        * Checks if we are the leader, exiting early with WaitingStatus if we are not
        * Upgrades the Istio control plane if changes were made to the CNI plugin configurations
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

        # Call upgrade_charm in case there are new configurations that affect the control plane
        # only if the CNI configurations have been provided and have changed from a previous state
        # This is useful when there is a missing configuration during the install process
        if self._cni_config_changed():
            self.upgrade_charm(event)

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

    def remove(self, event):
        """Remove charm, the Kubernetes resources it uses, and the istio specific resources."""
        istioctl = Istioctl(ISTIOCTL_PATH, self.model.name, ISTIOCTL_DEPOYMENT_PROFILE)
        handled_errors = []
        try:
            self._reconcile_gateway()
            ingress_data = self._get_ingress_data(event)
            self._reconcile_ingress(ingress_data)
            ingress_auth_data = self._get_ingress_auth_data(event)
            self._reconcile_ingress_auth(ingress_auth_data)
            istioctl.remove()
        except (ApiError, ErrorWithStatus, IstioctlError) as error:
            handled_errors.append(error)

        if handled_errors:
            self._report_handled_errors(errors=handled_errors)

    def upgrade_charm(self, _):
        """Upgrade charm.

        Supports upgrade of exactly one minor version at a time.
        """
        self._log_and_set_status(MaintenanceStatus("Upgrading Istio control plane."))

        istioctl = Istioctl(
            ISTIOCTL_PATH,
            self.model.name,
            ISTIOCTL_DEPOYMENT_PROFILE,
            istioctl_extra_flags=self._istioctl_extra_flags,
        )

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

        self.log.info("Upgrade complete.")
        self.unit.status = ActiveStatus()

    def _check_leader(self):
        """Check if this unit is a leader."""
        if not self.unit.is_leader():
            self.log.info("Not a leader, skipping setup")
            raise ErrorWithStatus("Waiting for leadership", WaitingStatus)

    @property
    def _cert_subject(self) -> Optional[str]:
        """Return the certificate subject to be used in the CSR.

        If the csr-domain-name configuration option is set, this value is used for both
        cert_subject and sans; otherwise, use the IP address or hostname of the actual
        ingress gateway service. Lastly, if for any reason the service address cannot be
        retrieved, None will be returned.
        """
        # Prioritise the csr-domain-name config option
        if csr_domain_name := self.model.config["csr-domain-name"]:
            return csr_domain_name

        # Get the ingress gateway service address
        try:
            svc = self._get_gateway_service()
        except ApiError:
            self.log.info("Could not retrieve the gateway service address.")
            return None

        svc_address = _get_gateway_address_from_svc(svc)

        # NOTE: returning None here means that the CSR will have the unit name as cert subject
        # this is an implementation detail of the cert_hanlder library.
        return svc_address if svc_address else None

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

            # Go into waiting status if there is no ingress-auth data
            if not ingress_auth_data:
                raise ErrorWithStatus("Waiting for the auth provider data.", WaitingStatus)

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

        # The app-level data may still be visible on a broken relation, but we
        # shouldn't be keeping the VirtualService for that related app.
        if isinstance(event, RelationBrokenEvent) and event.relation.name == INGRESS_RELATION_NAME:
            if event.app is None:
                self.log.info(
                    f"When handling event '{event}', event.app found to be None.  We cannot pop"
                    f" the departing application's data from 'routes' because we do not know the"
                    f" departing application's name.  We assume that the departing application's"
                    f" is not in routes.keys='{list(routes.keys())}'."
                )
            elif routes:
                routes.pop((event.relation, event.app), None)

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
        """Creates or updates the Gateway resources."""
        context = {
            "gateway_name": self._gateway_name,
            "namespace": self._gateway_namespace,
            "port": self._gateway_port,
            "tls_crt": None,
            "tls_key": None,
            "secure": False,
        }

        # Secure the gateway, if certificates relation is enabled and
        # both the CA cert and key are provided
        if self._use_https():
            self._log_and_set_status(MaintenanceStatus("Setting TLS Ingress"))
            context["tls_crt"] = self._tls_info["tls-crt"]
            context["tls_key"] = self._tls_info["tls-key"]
            context["secure"] = True

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

    @property
    def _tls_info(self) -> Dict[str, str]:
        """Return a dictionary with TLS cert and key values.

        The dictionary is built based on available information, if
        the istio-tls-secret is found, it is prioritised and returned;
        otherwise, the information shared by a TLS certificate provider.
        """

        if self._use_https_with_tls_secret():
            tls_secret = self.model.get_secret(id=self._tls_secret_id)
            return {
                "tls-crt": base64.b64encode(
                    tls_secret.get_content(refresh=True)["tls-crt"].encode("ascii")
                ).decode("utf-8"),
                "tls-key": base64.b64encode(
                    tls_secret.get_content(refresh=True)["tls-key"].encode("ascii")
                ).decode("utf-8"),
            }
        return {
            "tls-crt": base64.b64encode(self._cert_handler.server_cert.encode("ascii")).decode(
                "utf-8"
            ),
            "tls-key": base64.b64encode(self._cert_handler.private_key.encode("ascii")).decode(
                "utf-8"
            ),
        }

    def _use_https(self) -> bool:
        """Return True if only one of the TLS configurations are enabled, False if none are.

        Raises:
            ErrorWithStatus: if both configurations are enabled.
        """
        if self._use_https_with_tls_provider() and self._use_https_with_tls_secret():
            self.log.error(
                "Only one TLS configuration is supported at a time."
                "Either remove the TLS certificate provider relation,"
                "or the tls-secret-id configuration option."
            )
            raise ErrorWithStatus(
                "Only one TLS configuration is supported at a time. See logs for details.",
                BlockedStatus,
            )
        return self._use_https_with_tls_provider() or self._use_https_with_tls_secret()

    def _use_https_with_tls_secret(self) -> bool:
        """Return True if tls-secret-id has been configured, False otherwise.

        Raises:
            ErrorWithStatus("tls-secret-id was provided, but the secret could not be found - ...):
                if a secret ID is passed through the tls-secret-id configuration option, but the
                secret cannot be found.
            ErrorWithStatus("Access to the istio-tls-secret must be granted."):
                if the secret ID is passed, but the access to the secret is not granted.
            ErrorWithStatus(Missing TLS value(s), please add them to the secret")
                if any of the TLS values are missing from the secret.
        """

        # Check if the tls-secret-id holds any value and get the secret
        if not self._tls_secret_id:
            return False

        try:
            secret = self.model.get_secret(id=self._tls_secret_id)
        except SecretNotFoundError:
            raise ErrorWithStatus(
                "tls-secret-id was provided, but the secret could not be found - "
                "please provide a secret ID of a secret that exists.",
                BlockedStatus,
            )
        # FIXME: right now, juju will raise a ModelError when the application hasn't been
        # granted access to the secret, but because of https://bugs.launchpad.net/juju/+bug/2067336
        # this behaviour will change. Once that is done, we must ensure we reflect the change by
        # removing this exception and add a new one that covers the actual behaviour.
        # See canonical/istio-operators#420 for more details
        except ModelError as model_error:
            if "ERROR permission denied" in model_error.args[0]:
                # Block the unit when there is an ERROR permission denied
                raise ErrorWithStatus(
                    (
                        f"Permission denied trying to access TLS secret.\n"
                        f"Access to the secret with id: {self._tls_secret_id} must be granted."
                        " See juju grant-secret --help for details on granting permission."
                    ),
                    BlockedStatus,
                )

        try:
            tls_key = secret.get_content(refresh=True)["tls-key"]
            tls_crt = secret.get_content(refresh=True)["tls-crt"]
        except KeyError as err:
            self.log.error(
                f"Cannot configure TLS - Missing TLS {err.args} value(s), "
                "make sure they are passed as contents of the TLS secret."
            )
            raise ErrorWithStatus(
                f"Missing TLS {err.args} value(s), please add them to the TLS secret",
                BlockedStatus,
            )

        # If both the TLS key and cert are provided, we configure TLS
        if tls_key and tls_crt:
            return True

    # ---- End of the block

    def _use_https_with_tls_provider(self) -> bool:
        """Return True if TLS key and cert are provided by a TLS cert provider, False otherwise.

        Raises:
            ErrorWithStatus: if one of the values is missing.
        """

        # Immediately return False if the CertHandler is not enabled
        if not self._cert_handler.enabled:
            return False

        # If the certificates relation is established, we can assume
        # that we want to configure TLS
        if _xor(self._cert_handler.server_cert, self._cert_handler.private_key):
            # Fail if tls is only partly configured as this is probably a mistake
            missing = "pkey"
            if not self._cert_handler.server_cert:
                missing = "CA cert"
            raise ErrorWithStatus(
                f"Missing {missing}, cannot configure TLS",
                BlockedStatus,
            )
        if self._cert_handler.server_cert and self._cert_handler.private_key:
            return True

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

    def _check_cni_configurations(self) -> bool:
        """Return True if the necessary CNI configuration options are set, False otherwise."""
        return self.model.config["cni-conf-dir"] and self.model.config["cni-bin-dir"]

    def _cni_config_changed(self):
        """
        Returns True if any of the CNI configuration options has changed from a previous state,
        False otherwise.
        """
        # The peer relation is required to store values, if it does not exist because it was
        # removed by accident, the charm should fail
        rel = self.model.get_relation(self.peer_relation_name, None)
        if not rel:
            raise GenericCharmRuntimeError(
                "The istio-pilot charm requires a peer relation, make sure it exists."
            )

        # Get current values of the configuration options
        current_cni_bin_dir = rel.data[self.unit].get("cni-bin-dir", None)
        current_cni_conf_dir = rel.data[self.unit].get("cni-conf-dir", None)

        # Update the values based on the configuration options
        rel.data[self.unit].update({"cni-bin-dir": self.model.config["cni-bin-dir"]})
        rel.data[self.unit].update({"cni-conf-dir": self.model.config["cni-conf-dir"]})

        new_cni_bin_dir = rel.data[self.unit].get("cni-bin-dir", None)
        new_cni_conf_dir = rel.data[self.unit].get("cni-conf-dir", None)

        # Compare current vs new values and decide whether they have changed from a previous state
        cni_bin_dir_changed = False
        cni_conf_dir_changed = False
        if current_cni_bin_dir != new_cni_bin_dir:
            cni_bin_dir_changed = True

        if current_cni_conf_dir != new_cni_conf_dir:
            cni_conf_dir_changed = True

        # If any of the configuration options changed, return True
        return cni_bin_dir_changed or cni_conf_dir_changed


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
    elif svc.spec.type == "NodePort":
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
