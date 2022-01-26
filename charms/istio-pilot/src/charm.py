#!/usr/bin/env python3

import logging
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces


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

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)

        self.framework.observe(self.on["istio-pilot"].relation_changed, self.send_info)

        self.framework.observe(self.on['ingress'].relation_changed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_departed, self.handle_ingress)
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

        subprocess.run(
            ["./kubectl", "delete", "-f-", "--ignore-not-found"],
            input=manifests,
            # Can't remove stuff yet: https://bugs.launchpad.net/juju/+bug/1941655
            # check=True,
        )

    def send_info(self, event):
        if self.interfaces["istio-pilot"]:
            self.interfaces["istio-pilot"].send_data(
                {
                    "service-name": f'istiod.{self.model.name}.svc',
                    "service-port": '15012',
                }
            )

    def handle_ingress(self, event):
        ingress = self.interfaces['ingress']

        if ingress:
            routes = ingress.get_data().items()
        else:
            routes = []

        t = self.env.get_template('virtual_service.yaml.j2')
        default_gateway = self.model.config['default-gateways'].split(',')[0]

        def get_kwargs(version, route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {"default_gateway": default_gateway, **route}

            if 'namespace' not in kwargs:
                kwargs['namespace'] = self.model.name

            return kwargs

        custom_gateways = list(filter(None, self.config["custom-gateways"].split(",")))

        virtual_services = ''.join(
            t.render(
                **get_kwargs(ingress.versions[app.name], route), custom_gateways=custom_gateways
            )
            for ((_, app), route) in routes
        )

        t = self.env.get_template('gateway.yaml.j2')
        gateways = self.model.config['default-gateways'].split(',')
        gateways = ''.join(t.render(name=g) for g in gateways)

        manifests = [virtual_services, gateways]
        manifests = '\n'.join([m for m in manifests if m])

        subprocess.run(
            [
                './kubectl',
                'delete',
                'virtualservices,gateways',
                f'-lapp.juju.is/created-by={self.model.app.name}',
                '-n',
                self.model.name,
            ],
            check=True,
        )
        subprocess.run(
            ["./kubectl", "apply", "-f-"],
            input=manifests.encode('utf-8'),
            check=True,
        )

    def handle_ingress_auth(self, event):
        auth_routes = self.interfaces['ingress-auth']
        if auth_routes:
            auth_routes = list(auth_routes.get_data().values())
        else:
            auth_routes = []

        if not all(ar.get("service") for ar in auth_routes):
            self.model.unit.status = WaitingStatus("Waiting for auth route connection information.")
            return

        rbac_configs = Path('src/rbac_config.yaml').read_text() if auth_routes else None

        t = self.env.get_template('auth_filter.yaml.j2')
        auth_filters = ''.join(
            t.render(
                namespace=self.model.name,
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

        manifests = [rbac_configs, auth_filters]
        manifests = '\n'.join([m for m in manifests if m])
        subprocess.run(
            [
                './kubectl',
                'delete',
                'envoyfilters,rbacconfigs',
                f'-lapp.juju.is/created-by={self.model.app.name}',
                '-n',
                self.model.name,
            ],
            check=True,
        )

        subprocess.run(
            ["./kubectl", "apply", "-f-"],
            input=manifests.encode('utf-8'),
            check=True,
        )


if __name__ == "__main__":
    main(Operator)
