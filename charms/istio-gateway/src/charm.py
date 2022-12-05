#!/usr/bin/env python3

import logging

from jinja2 import Environment, FileSystemLoader
from lightkube import Client, codecs
from lightkube.core.exceptions import ApiError
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, StatusBase, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.log = logging.getLogger(__name__)

        # Every lightkube API call will use the model name as the namespace by default
        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")

        for event in [
            self.on.start,
            self.on["istio-pilot"].relation_changed,
            self.on.config_changed,
        ]:
            self.framework.observe(event, self.start)
        self.framework.observe(self.on.remove, self.remove)

    def start(self, event):
        """Event handler for StartEevnt."""
        try:
            self._check_leader()

            interfaces = self._get_interfaces()

        except CheckFailed as error:
            self.model.unit.status = error.status
            return

        if self.model.config["kind"] not in ("ingress", "egress"):
            self.model.unit.status = BlockedStatus("Config item `kind` must be set")
            return

        if not self.model.relations["istio-pilot"]:
            self.model.unit.status = BlockedStatus("Waiting for istio-pilot relation")
            return

        if not ((pilot := interfaces["istio-pilot"]) and pilot.get_data()):
            self.model.unit.status = WaitingStatus(
                "Waiting for istio-pilot relation data, deferring event"
            )
            event.defer()
            return

        if self.model.config["gateway_service_type"] not in ("LoadBalancer", "ClusterIP"):
            self.model.unit.status = BlockedStatus(
                "Ingress GW svc must be of type: LoadBalancer, ClusterIP"
            )
            return

        pilot = list(pilot.get_data().values())[0]

        env = Environment(loader=FileSystemLoader("src"))
        template = env.get_template("manifest.yaml")
        rendered = template.render(
            kind=self.model.config["kind"],
            namespace=self.model.name,
            pilot_host=pilot["service-name"],
            pilot_port=pilot["service-port"],
            gateway_service_type=self.model.config["gateway_service_type"],
        )

        for obj in codecs.load_all_yaml(rendered):
            self.log.debug(f"Deploying {obj.metadata.name} of kind {obj.kind}")
            self.lightkube_client.apply(obj, namespace=obj.metadata.namespace)

        self.unit.status = ActiveStatus()

    def remove(self, event):
        """Remove charm."""

        env = Environment(loader=FileSystemLoader("src"))
        template = env.get_template("manifest.yaml")
        rendered = template.render(
            kind=self.model.config["kind"],
            namespace=self.model.name,
            pilot_host="foo",
            pilot_port="foo",
        )

        try:
            for obj in codecs.load_all_yaml(rendered):
                self.lightkube_client.delete(
                    type(obj), obj.metadata.name, namespace=obj.metadata.namespace
                )
        except ApiError as err:
            self.log.exception("ApiError encountered while attempting to delete resource.")
            if err.status.message is not None:
                if "(Unauthorized)" in err.status.message:
                    # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
                    self.log.error(
                        f"Ignoring unauthorized error during cleanup:" f"\n{err.status.message}"
                    )
                else:
                    # But surface any other errors
                    self.log.error(err.status.message)
                    raise
            else:
                raise

    def _check_leader(self):
        if not self.unit.is_leader():
            raise CheckFailed("Waiting for leadership", WaitingStatus)

    def _get_interfaces(self):
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise CheckFailed(str(err), WaitingStatus)
        except NoCompatibleVersions as err:
            raise CheckFailed(str(err), BlockedStatus)
        return interfaces


class CheckFailed(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=StatusBase):
        super().__init__()

        self.msg = str(msg)
        self.status_type = status_type
        self.status = status_type(self.msg)


if __name__ == "__main__":
    main(Operator)
