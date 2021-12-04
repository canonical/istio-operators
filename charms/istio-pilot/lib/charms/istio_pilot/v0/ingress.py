# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""# Ingress Interface Library.
"""

import logging
from functools import cached_property

from ops.charm import CharmBase
from ops.framework import Object
from ops.model import BlockedStatus, WaitingStatus
from serialized_data_interface import get_interface, NoCompatibleVersions, NoVersionsListed

logger = logging.getLogger(__name__)

# The unique Charmhub library identifier, never change it
LIBID = "b521889515b34432b952f75c21e96dfc"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


class IngressRequirer(Object):
    def __init__(self, charm: CharmBase, relation_endpoint: str):
        """Constructor for IngressRequirer.

        Args:
            charm: the charm that is instantiating the library.
            relation_endpoint: the name of the relation endpoint to bind to
                (must be of interface type "ingress")
        """
        super().__init__(charm, f"ingress-requirer-{relation_endpoint}")
        self.charm = charm
        self.relation_endpoint = relation_endpoint

    def request(
        self,
        *,
        port: int,
        service: str = None,
        prefix: str = None,
        rewrite: str = None,
        namespace: str = None,
        per_unit_routes: bool = False,
    ):
        """Request ingress to a service.

        Args:
            service: the name of the target K8s service to route to; defaults to the
                charm's automatically created service (i.e., the application name)
            port: the port of the service (required)
            prefix: the path used to match this service for requests to the gateway;
                must not conflict with other services; defaults to f"/{service}/"
            rewrite: the path on the target service to map the request to; defaults
                to "/"
            namespace: the namespace the service is in; default to the current model
            per_unit_routes: whether or not to create URLs which map to specific units;
                the URLs will have their own prefix of f"{prefix}-unit-{unit_num}" (with
                tailing slashes handled appropriately)
        """
        if not self.charm.unit.is_leader():
            raise RequestFailed(
                WaitingStatus(f"Only leader can request ingress: {self.relation_endpoint}")
            )
        try:
            ingress = get_interface(self.charm, self.relation_endpoint)
            if not ingress:
                raise RequestFailed(BlockedStatus(f"Missing relation: {self.relation_endpoint}"))
        except NoCompatibleVersions:
            raise RequestFailed(
                BlockedStatus(f"Relation version not compatible: {self.relation_endpoint}")
            )
        except NoVersionsListed:
            raise RequestFailed(WaitingStatus(f"Waiting on relation: {self.relation_endpoint}"))
        ingress.send_data(
            {
                "namespace": namespace or self.model.name,
                "prefix": prefix or f"/{self.charm.app.name}/",
                "rewrite": rewrite or "/",
                "service": service or self.app.name,
                "port": port,
                "per_unit_routes": per_unit_routes,
            },
        )

    @cached_property
    def url(self):
        """The full ingress URL to reach the target service by.

        May return None if the URL isn't available yet.
        """
        try:
            ingress = get_interface(self.charm, self.relation_endpoint)
            if not ingress:
                return None
        except (NoCompatibleVersions, NoVersionsListed):
            return None
        all_data = ingress.get_data()
        for (rel, app), data in all_data.items():
            if app is self.charm.app:
                continue
            return data["url"]
        else:
            return None

    @cached_property
    def unit_urls(self):
        """The full ingress URLs which map to each indvidual unit.

        May return None if the URLs aren't available yet, or if per-unit routing
        was not requested. Otherwise, returns a map of unit name to URL.
        """
        try:
            ingress = get_interface(self.charm, self.relation_endpoint)
            if not ingress:
                return None
        except (NoCompatibleVersions, NoVersionsListed):
            return None
        all_data = ingress.get_data()
        for (rel, app), data in all_data.items():
            if app is self.charm.app:
                continue
            return data.get("unit_urls")
        else:
            return None


class RequestFailed(Exception):
    def __init__(self, status):
        self.status = status
