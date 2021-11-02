#!/usr/bin/env python3

import logging
import subprocess

from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase
from ops.main import main
from ops.model import BlockedStatus, WaitingStatus
from serialized_data_interface import NoVersionsListed, get_interfaces


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.log = logging.getLogger(__name__)

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on["istio-pilot"].relation_changed, self.install)
        self.framework.observe(self.on.config_changed, self.install)
        self.framework.observe(self.on.remove, self.remove)

    def install(self, event):
        """Install charm."""

        if not self.unit.is_leader():
            return self.unit, WaitingStatus("Waiting for leadership")

        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            return self.app, WaitingStatus(str(err))

        if self.model.config['kind'] not in ('ingress', 'egress'):
            return self.app, BlockedStatus('Config item `kind` must be set')

        if not self.model.relations['istio-pilot']:
            return self.app, BlockedStatus("Waiting for istio-pilot relation")

        if not ((pilot := self.interfaces["istio-pilot"]) and pilot.get_data()):
            return self.app, BlockedStatus("Waiting for istio-pilot relation data")

        pilot = list(pilot.get_data().values())[0]

        env = Environment(loader=FileSystemLoader('src'))
        template = env.get_template('manifest.yaml')
        rendered = template.render(
            kind=self.model.config['kind'],
            namespace=self.model.name,
            pilot_host=pilot['service-name'],
            pilot_port=pilot['service-port'],
        )

        subprocess.run(["./kubectl", "apply", "-f-"], input=rendered.encode('utf-8'), check=True)

    def remove(self, event):
        """Remove charm."""

        env = Environment(loader=FileSystemLoader('src'))
        template = env.get_template('manifest.yaml')
        rendered = template.render(
            kind=self.model.config['kind'],
            namespace=self.model.name,
            pilot_host='foo',
            pilot_port='foo',
        )

        subprocess.run(
            ["./kubectl", "delete", "-f-"],
            input=rendered.encode('utf-8'),
            # Can't remove stuff yet: https://bugs.launchpad.net/juju/+bug/1941655
            # check=True
        )


if __name__ == "__main__":
    main(Operator)
