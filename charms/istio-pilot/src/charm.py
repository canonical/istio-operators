#!/usr/bin/env python3

import logging
import subprocess

import yaml
from jinja2 import Environment, FileSystemLoader
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.resources.core_v1 import Service
from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces

from istio_gateway_info_provider import RELATION_NAME, GatewayProvider
from resources_handler import ResourceHandler

GATEWAY_HTTP_PORT = 8080
GATEWAY_HTTPS_PORT = 8443
METRICS_PORT = 15014
GATEWAY_WORKLOAD_SERVICE_NAME = "istio-ingressgateway-workload"


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

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

        self.log = logging.getLogger(__name__)

        self.env = Environment(loader=FileSystemLoader('src'))
        self._resource_handler = ResourceHandler(self.app.name, self.model.name)

        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")
        self._resource_files = [
            "gateway.yaml.j2",
            "auth_filter.yaml.j2",
            "virtual_service.yaml.j2",
        ]
        self.gateway = GatewayProvider(self)

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)

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

        def get_kwargs(version, route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {"gateway": gateway, "app_name": self.app.name, **route}

            if "namespace" not in kwargs:
                kwargs["namespace"] = self.model.name

            return kwargs

        # TODO: we could probably extract the rendering bits from the charm code
        virtual_services = "\n---".join(
            t.render(**get_kwargs(ingress.versions[app.name], route)).strip().strip("---")
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

        t = self.env.get_template("auth_filter.yaml.j2")
        auth_filters = "".join(
            t.render(
                namespace=self.model.name,
                app_name=self.app.name,
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


if __name__ == "__main__":
    main(Operator)
