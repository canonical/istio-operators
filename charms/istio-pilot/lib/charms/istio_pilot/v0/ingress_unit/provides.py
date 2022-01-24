import logging
from functools import cache
from pathlib import Path
from typing import Dict

import sborl
from ops.charm import CharmBase
from ops.model import Relation, Unit


log = logging.getLogger(__name__)


class IngressUnitProvider(sborl.EndpointWrapper):
    ROLE = "provides"
    INTERFACE = "ingress-per-unit"
    SCHEMA = Path(__file__).parent / "schema.yaml"

    def __init__(self, charm: CharmBase, relation_name: str = None):
        super().__init__(charm, relation_name)

    def get_request(self, relation: Relation):
        """Get the IngressRequest for the given Relation."""
        return IngressRequest(self, relation)

    @cache
    def is_failed(self, relation: Relation = None):
        if relation is None:
            return any(self.is_failed(relation) for relation in self.relations)
        if not relation.units:
            return False
        if super().is_failed(relation):
            return True
        data = self.unwrap(relation)
        prev_fields = None
        for unit in relation.units:
            if not data[unit]:
                continue
            new_fields = {field: data[unit][field] for field in ("model", "port", "rewrite")}
            if prev_fields is None:
                prev_fields = new_fields
            if new_fields != prev_fields:
                raise RelationDataMismatchError(relation, unit)
        return False


class IngressRequest:
    def __init__(self, provider: IngressUnitProvider, relation: Relation):
        self._provider = provider
        self._relation = relation
        self._data = provider.unwrap(relation)

    @property
    def app(self):
        """The remote application."""
        return self._relation.app

    @property
    def units(self):
        """The remote units."""
        return sorted(self._relation.units, key=lambda unit: unit.name)

    @property
    def model(self):
        """The name of the model the request was made from."""
        return self._data[self.units[0]]["model"]

    @property
    def service(self):
        return self._data[self.units[0]]["name"].split("/")[0]

    @property
    def port(self):
        """The backend port."""
        return self._data[self.units[0]]["port"]

    @property
    def rewrite(self):
        """The backend path."""
        return self._data[self.units[0]]["rewrite"]

    def get_prefix(self, unit: Unit):
        """The prefix for a given unit."""
        return self._data[unit]["prefix"]

    def get_ip(self, unit: Unit):
        return self._data[unit]["ip"]

    def send_urls(self, urls: Dict[Unit, str]):
        """Send URLs back for the request.

        Note: only the leader can send URLs.
        """
        self._provider.wrap(
            self._relation,
            {
                self.charm.app: {
                    "urls": {self._data[unit]["name"]: url for unit, url in urls.items()}
                }
            },
        )


class RelationDataMismatchError(sborl.errors.RelationDataError):
    """Data from different units do not match where they should."""
