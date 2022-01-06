#!/usr/bin/env python3

import logging
import subprocess

import yaml
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces
from lightkube import Client, codecs
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource


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

        # Every lightkube API call will use the model name as the namespace by default
        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")
        # Create namespaced resource classes for lightkube client
        # This is necessary for lightkube to interact with custom resources
        self.envoy_filter_resource = create_namespaced_resource(
            group="networking.istio.io",
            version="v1alpha3",
            kind="EnvoyFilter",
            plural="envoyfilters",
            verbs=None,
        )

        self.virtual_service_resource = create_namespaced_resource(
            group="networking.istio.io",
            version="v1alpha3",
            kind="VirtualService",
            plural="virtualservices",
            verbs=None,
        )

        self.gateway_resource = create_namespaced_resource(
            group="networking.istio.io",
            version="v1beta1",
            kind="Gateway",
            plural="gateways",
            verbs=None,
        )

        self.rbac_config_resource = create_namespaced_resource(
            group="rbac.istio.io",
            version="v1alpha1",
            kind="RbacConfig",
            plural="rbacconfigs",
            verbs=None,
        )

        self.env = Environment(loader=FileSystemLoader('src'))

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)

        self.framework.observe(self.on.config_changed, self.handle_default_gateways)

        self.framework.observe(self.on["istio-pilot"].relation_changed, self.send_info)

        self.framework.observe(self.on['ingress'].relation_changed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_departed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_broken, self.handle_ingress)
        self.framework.observe(self.on['ingress-auth'].relation_changed, self.handle_ingress_auth)
        self.framework.observe(self.on['ingress-auth'].relation_departed, self.handle_ingress_auth)

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

        for resource in [
            self.virtual_service_resource,
            self.gateway_resource,
            self.envoy_filter_resource,
            self.rbac_config_resource,
        ]:
            self._delete_existing_resource_objects(
                resource, namespace=self.model.name, ignore_unauthorized=True
            )
        self._delete_manifest(
            manifests, namespace=self.model.name, ignore_not_found=True, ignore_unauthorized=True)

    def handle_default_gateways(self, event):
        t = self.env.get_template('gateway.yaml.j2')
        gateways = self.model.config['default-gateways'].split(',')
        manifest = ''.join(t.render(name=g, app_name=self.app.name) for g in gateways)
        self._kubectl(
            'delete',
            'gateways',
            f"-lapp.juju.is/created-by={self.app.name},"
            f"app.{self.app.name}.io/is-workload-entity=true",
        self._kubectl("apply", "-f-", input=manifest)

    def send_info(self, event):
        if self.interfaces["istio-pilot"]:
            self.interfaces["istio-pilot"].send_data(
                {"service-name": f'istiod.{self.model.name}.svc', "service-port": '15012'}
            )

    def handle_ingress(self, event):
        gateway_address = self._get_gateway_address()
        if not gateway_address:
            self.unit.status = WaitingStatus("Waiting for gateway address")
            event.defer()
            return
        else:
            self.unit.status = ActiveStatus()

        ingress = self.interfaces['ingress']

        if ingress:
            routes = ingress.get_data().items()
        else:
            routes = {}

        if isinstance(event, RelationBrokenEvent):
            # The app-level data is still visible on a broken relation, but we
            # shouldn't be keeping the VirtualService for that related app.
            del routes[(event.relation, event.app)]

        t = self.env.get_template('virtual_service.yaml.j2')
        gateway = self.model.config['default-gateways'].split(',')[0]

        def get_kwargs(version, route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {'gateway': gateway, 'app_name': self.app.name, **route}

            if 'namespace' not in kwargs:
                kwargs['namespace'] = self.model.name

            return kwargs

        virtual_services = '\n---'.join(
            t.render(**get_kwargs(ingress.versions[app.name], route)).strip().strip("---")
            for ((_, app), route) in routes
        )

        t = self.env.get_template('gateway.yaml.j2')
        gateways = self.model.config['default-gateways'].split(',')
        gateways = ''.join(t.render(name=g) for g in gateways)

        manifests = [virtual_services, gateways]
        manifests = '\n'.join([m for m in manifests if m])

        for resource in [self.virtual_service_resource, self.gateway_resource]:
            self._delete_existing_resource_objects(resource, namespace=self.model.name)

        self._apply_manifest(manifests, namespace=self.model.name)

    def handle_ingress_auth(self, event):
        auth_routes = self.interfaces['ingress-auth']
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

        t = self.env.get_template('auth_filter.yaml.j2')
        auth_filters = ''.join(
            t.render(
                namespace=self.model.name,
                app_name=self.app.name,
                **{
                    'request_headers': yaml.safe_dump(
                        [{'exact': h} for h in r.get('allowed-request-headers', [])],
                        default_flow_style=True,
                    ),
                    'response_headers': yaml.safe_dump(
                        [{'exact': h} for h in r.get('allowed-response-headers', [])],
                        default_flow_style=True,
                    ),
                    'port': r['port'],
                    'service': r['service'],
                },
            )
            for r in auth_routes
        )

        manifests = [auth_filters]
        manifests = '\n'.join([m for m in manifests if m])

        for resource in [self.envoy_filter_resource, self.rbac_config_resource]:
            self._delete_existing_resource_objects(resource, namespace=self.model.name)
        self._apply_manifest(manifests, namespace=self.model.name)

    def _delete_object(
        self, obj, namespace=None, ignore_not_found=False, ignore_unauthorized=False
    ):
        try:
            self.lightkube_client.delete(type(obj), obj.metadata.name, namespace=namespace)
        except ApiError as err:
            self.log.exception("ApiError encountered while attempting to delete resource.")
            if err.status.message is not None:
                if "not found" in err.status.message and ignore_not_found:
                    self.log.error(f"Ignoring not found error:\n{err.status.message}")
                elif "(Unauthorized)" in err.status.message and ignore_unauthorized:
                    # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
                    self.log.error(f"Ignoring unauthorized error:\n{err.status.message}")
                else:
                    self.log.error(err.status.message)
                    raise
            else:
                raise

    def _delete_existing_resource_objects(
        self, resource, namespace=None, ignore_not_found=False, ignore_unauthorized=False
    ):
        for obj in self.lightkube_client.list(
            resource, labels={"app.juju.is/created-by": f"{self.app.name}"}
        ):
            self._delete_object(
                obj,
                namespace=namespace,
                ignore_not_found=ignore_not_found,
                ignore_unauthorized=ignore_unauthorized,
            )

    def _apply_manifest(self, manifest, namespace=None):
        for obj in codecs.load_all_yaml(manifest):
            self.lightkube_client.apply(obj, namespace=namespace)

    def _delete_manifest(
        self, manifest, namespace=None, ignore_not_found=False, ignore_unauthorized=False
    ):
        for obj in codecs.load_all_yaml(manifest):
            self._delete_object(
                obj,
                namespace=namespace,
                ignore_not_found=ignore_not_found,
                ignore_unauthorized=ignore_unauthorized,
            )


if __name__ == "__main__":
    main(Operator)
