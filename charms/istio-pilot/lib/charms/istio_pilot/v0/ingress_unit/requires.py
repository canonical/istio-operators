# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from functools import cached_property
from pathlib import Path

import sborl
from ops.charm import CharmBase

logger = logging.getLogger(__name__)


class IngressUnitRequirer(sborl.EndpointWrapper):
    ROLE = "requires"
    INTERFACE = "ingress-per-unit"
    SCHEMA = Path(__file__).parent / "schema.yaml"
    LIMIT = 1

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = None,
        *,
        port: int = None,
        rewrite: str = None,
    ):
        """Constructor for IngressRequirer.

        The request args can be used to specify the ingress properties when the
        instance is created. If any are set, at least `port` is required, and
        they will be sent to the ingress provider as soon as it is available.
        All request args must be given as keyword args.

        Args:
            charm: the charm that is instantiating the library.
            relation_name: the name of the relation endpoint to bind to
                (defaults to "ingress-per-unit"; relation must be of interface type
                "ingress-per-unit" and have "limit: 1")
        Request Args:
            port: the port of the service (required)
            rewrite: the path on the target service to map the request to; defaults
                to "/"
        """
        super().__init__(charm, relation_name)
        self.auto_data = self._get_data(port, rewrite)

    def _get_data(self, port: int, rewrite: str):
        unit_name_dashed = self.charm.unit.name.replace("/", "-")
        binding = self.charm.model.get_binding(self.relation_name)
        return {
            self.charm.unit: {
                "model": self.model.name,
                "name": self.charm.unit.name,
                "ip": binding.network.bind_address,
                "prefix": f"{self.model.name}-{unit_name_dashed}",
                "port": port,
                "rewrite": rewrite or "/",
            },
        }

    def request(self, *, port: int, rewrite: str = None):
        """Request ingress to this unit.

        Args:
            port: the port of the service (required)
            rewrite: the path on the target unit to map the request to; defaults
                to "/"
        """
        self.wrap(self.relation, self._get_data(port, rewrite))

    @property
    def relation(self):
        return self.relations[0] if self.relations else None

    @cached_property
    def urls(self):
        """The full ingress URLs to reach every unit.

        May return an empty dict if the URLs aren't available yet.
        """
        if not self.is_ready():
            return None
        data = self.unwrap(self.relation)
        return dict(data[self.relation.app]["urls"])

    @cached_property
    def unit_url(self):
        """The full ingress URL to reach the current unit.

        May return None if the URL isn't available yet.
        """
        if not self.urls:
            return None
        return self.urls.get(self.charm.unit.name)
