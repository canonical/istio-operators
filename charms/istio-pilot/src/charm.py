#!/usr/bin/env python3

import logging
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus, StatusBase, ErrorStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        # This comes out cleaner if we update the serialized_data_interface.get_interfaces() to
        # return interfaces regardless of whether any interface raised an error.  Maybe it returns:
        # {'working_interface_A': SDI_instance, 'broken_interface_B': NoCompatibleVersions, ...}
        # This way we can interrogate interfaces independently
        # (This might be a breaking change, so maybe we'd want to add a second get_interfaces
        # method, bump the package to 0.4, or add an arg to the existing one)
        self.interfaces = get_interfaces(self)

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

        if not isinstance(is_leader := self._check_is_leader(), ActiveStatus):
            self.model.unit.status = is_leader
            return

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

        # Check that we are actually working
        self.update_status(event)

    def update_status(self, event):
        # This method should always result in a status being set to something as it follows things
        # like install's MaintenanceStatus

        ingress_status = self._get_ingress_status()
        ingress_auth_status = self._get_ingress_auth_status()
        # Do something to report these statuses in logs.  Maybe loop through and for any that
        # !=Active, we report in logs?  Could also use this to define an ActiveStatus message
        # eg ActiveStatus('Running with 1 ingress and 2 ingress_auth relations broken.'
        #                 'See `juju debug-log` for details')

        self.model.unit.status = self._get_application_status()
        # This could also try to fix a broken application if status != Active, and could fix broken
        # relations

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
        # TODO: This probably needs a check to ensure this interface exists
        if self.interfaces["istio-pilot"]:
            self.interfaces["istio-pilot"].send_data(
                {
                    "service-name": f'istiod.{self.model.name}.svc',
                    "service-port": '15012',
                }
            )

    def handle_ingress(self, event):
        if not isinstance(
                interface_status := validate_interface(
                    self.interfaces["ingress"]
                ),
                ActiveStatus,
        ):
            # Could raise status here, or just log
            self.model.unit.status = interface_status
            return

        ingress = self.interfaces['ingress']

        if ingress:
            routes = ingress.get_data().items()
        else:
            routes = []

        t = self.env.get_template('virtual_service.yaml.j2')
        gateway = self.model.config['default-gateways'].split(',')[0]

        def get_kwargs(version, route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {'gateway': gateway, **route}

            if 'namespace' not in kwargs:
                kwargs['namespace'] = self.model.name

            return kwargs

        virtual_services = ''.join(
            t.render(**get_kwargs(ingress.versions[app.name], route))
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

        self.update_status(event)

    def _get_ingress_status(self):
        # TODO: Loop through ingress relations, reporting their individual statuses
        #       Lots of this is common with `handle_ingress` and could be combined in a helper

        ingress = self.interfaces['ingress']

        if ingress:
            routes = ingress.get_data().items()
        else:
            routes = []

        route_status = {}

        for route in routes:
            # TODO: Check that k8s objects expected to exist actually exist for this route
            # Not real syntax:
            if route is ok:
                route_status[route.name] = ActiveStatus()
            else:
                # (or something appropriate)
                route_status[route.name] = BlockedStatus()
        return route_status

    def _get_ingress_auth_status(self):
        # TODO: Similar to _get_ingress_status
        raise NotImplementedError()

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

        self.update_status(event)

    def _check_is_leader(self) -> StatusBase:
        if not self.unit.is_leader():
            return WaitingStatus("Waiting for leadership")
        else:
            # Or we could return None. It felt odd that this function would return a status or None
            return ActiveStatus()

    def _get_application_status(self) -> StatusBase:
        # TODO: Do whatever checks are needed to confirm we are actually working correctly
        #       Maybe check for key deployments, etc?

        # Until we have a real check - cheat :)  Note that this needs to be fleshed out if actually
        # used by a relation hook
        return ActiveStatus()


def validate_interface(interface):
    if is_instance(interface, NoVersionsListed):
        return WaitingStatus(str(interface))
    elif is_instance(interface, NoCompatibleVersions):
        return BlockedStatus(str(interface))
    elif is_instance(interface, Exception):
        return ErrorStatus(f"Unexpected error: {str(interface)}")

    return ActiveStatus()


if __name__ == "__main__":
    main(Operator)
