# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""# Ingress Interface Library.
"""

import logging
from functools import cached_property
from pathlib import Path

import yaml

from ops.charm import CharmBase
from ops.framework import EventBase, EventSource, Object, ObjectEvents
from ops.model import BlockedStatus, WaitingStatus
from serialized_data_interface import (
    get_schema,
    SerializedDataInterface,
    NoCompatibleVersions,
    NoVersionsListed,
)

logger = logging.getLogger(__name__)

# The unique Charmhub library identifier, never change it
LIBID = "b521889515b34432b952f75c21e96dfc"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


SCHEMA_URL_BASE = "https://raw.githubusercontent.com/canonical/operator-schemas"
# SCHEMA_URL = f"{SCHEMA_URL_BASE}/master/ingress.yaml"
SCHEMA_URL = f"{SCHEMA_URL_BASE}/1ed74c640bc289b71f261cda67177ee5209a1562/ingress.yaml"
SCHEMA_FILE = Path(__file__).parent / "ingress.yaml"
SCHEMA_VERSIONS = {"v3"}

DEFAULT_RELATION_NAME = "ingress"


class IngressProviderAvailableEvent(EventBase):
    """Event triggered when the ingress provider is ready for requests."""


class IngressReadyEvent(EventBase):
    """Event triggered when the ingress provider has returned the requested URL(s)."""


class IngressFailedEvent(EventBase):
    """Event triggered when something went wrong with the ingress relation."""


class IngressRemovedEvent(EventBase):
    """Event triggered when the ingress relation is removed."""


class IngressRequirerEvents(ObjectEvents):
    available = EventSource(IngressProviderAvailableEvent)
    ready = EventSource(IngressReadyEvent)
    failed = EventSource(IngressFailedEvent)
    removed = EventSource(IngressRemovedEvent)


class IngressRequirer(Object):
    on = IngressRequirerEvents()

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
        *,
        port: int = None,
        service: str = None,
        prefix: str = None,
        rewrite: str = None,
        namespace: str = None,
        per_unit_routes: bool = False,
    ):
        """Constructor for IngressRequirer.

        The request args can be used to specify the ingress properties when the
        instance is created. If any are set, at least `port` is required, and
        they will be sent to the ingress provider as soon as it is available.
        All request args must be given as keyword args.

        Args:
            charm: the charm that is instantiating the library.
            relation_name: the name of the relation endpoint to bind to
                (defaults to "ingress"; relation must be of interface type
                "ingress" and have "limit: 1")
        Request Args:
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
        super().__init__(charm, f"ingress-requirer-{relation_name}")
        self.charm = charm
        self.relation_name = relation_name

        self._validate_relation_meta()

        self._request_args = {
            "port": port,
            "service": service,
            "prefix": prefix,
            "rewrite": rewrite,
            "namespace": namespace,
            "per_unit_routes": per_unit_routes,
        }
        if any(self._request_args.values()) and not self._request_args["port"]:
            raise TypeError("Missing required argument: 'port'")

        self.framework.observe(charm.on[relation_name].relation_created, self._check_provider)
        self.framework.observe(charm.on[relation_name].relation_changed, self._check_provider)
        self.framework.observe(charm.on[relation_name].relation_broken, self._lost_provider)
        self.framework.observe(charm.on.leader_elected, self._check_provider)
        self.framework.observe(charm.on.upgrade_charm, self._check_upgrade)

    @cached_property
    def status(self):
        if not self.charm.model.relations[self.relation_name]:
            # the key will always exist but may be an empty list
            return BlockedStatus(f"Missing relation: {self.relation_name}")
        try:
            self._sdi
        except NoCompatibleVersions:
            return BlockedStatus(f"Relation version not compatible: {self.relation_name}")
        except NoVersionsListed:
            return WaitingStatus(f"Waiting on relation: {self.relation_name}")
        else:
            return None

    @cached_property
    def _sdi(self):
        """Get the SDI instance for the relation.

        This provides defaults for the schema URL and supported versions so that
        every client charm doesn't need to specify it, since they're already using
        the versioned library which is inherently tied to a schema & version. It
        looks for a local copy of the schema first, to allow it to be inlined during
        the build process to avoid a runtime network dependency.
        """
        if SCHEMA_FILE.exists():
            schema = yaml.safe_load(SCHEMA_FILE.read_text())
        else:
            schema = get_schema(SCHEMA_URL)
        return SerializedDataInterface(
            self.charm,
            self.relation_name,
            schema,
            SCHEMA_VERSIONS,
            "requires",
        )

    @property
    def is_available(self):
        return self.charm.unit.is_leader() and self.status is None

    @property
    def is_ready(self):
        return self.status is None and self.url

    def _check_provider(self, event):
        if self.is_ready:
            self.on.ready.emit()
        elif self.is_available:
            if any(self._request_args.values()):
                self.request(**self._request_args)
            self.on.available.emit()
        elif isinstance(self.status, BlockedStatus):
            self.on.failed.emit()

    def _check_upgrade(self, event):
        if self.is_available and any(self._request_args.values()):
            self.request(**self._request_args)

    def _lost_provider(self, event):
        # The relation technically still exists during the -broken hook, but we want
        # the status to reflect that it has gone away.
        self.status = BlockedStatus(f"Missing relation: {self.relation_name}")
        self.on.removed.emit()

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

        Note: only the leader unit can send the request.

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
                WaitingStatus(f"Only leader can request ingress: {self.relation_name}")
            )
        if self.status is not None:
            raise RequestFailed(self.status)
        self._sdi.send_data(
            {
                "namespace": namespace or self.model.name,
                "prefix": prefix or f"/{self.charm.app.name}/",
                "rewrite": rewrite or "/",
                "service": service or self.charm.app.name,
                "port": port,
                "per_unit_routes": per_unit_routes,
            },
        )

    @cached_property
    def _data(self):
        try:
            if not self._sdi:
                return None
        except (NoCompatibleVersions, NoVersionsListed):
            return None
        all_data = self._sdi.get_data()
        for (rel, app), data in all_data.items():
            if app is self.charm.app:
                continue
            if data.get("unit_urls"):
                # Workaround the fact that the other side of a CMR relation can't know
                # our proper unit name, by using the fact that the unit numbers will be
                # consistent, at least.
                data["unit_urls"] = {
                    f"{self.charm.app.name}/{unit_name.split('/')[-1]}": unit_url
                    for unit_name, unit_url in data["unit_urls"].items()
                }
            return data
        else:
            return {}

    @property
    def url(self):
        """The full ingress URL to reach the target service by.

        May return None if the URL isn't available yet.
        """
        return self._data.get("url")

    @property
    def unit_urls(self):
        """The full ingress URLs which map to each indvidual unit.

        May return None if the URLs aren't available yet, or if per-unit routing
        was not requested. Otherwise, returns a map of unit name to URL.
        """
        return self._data.get("unit_urls")

    @property
    def unit_url(self):
        """The full ingress URL which map to the current unit.

        May return None if the URLs aren't available yet, or if per-unit routing
        was not requested. Otherwise, returns a URL string.
        """
        return (self.unit_urls or {}).get(self.charm.unit.name)

    def _validate_relation_meta(self):
        """Validate that the relation is setup properly in the metadata."""
        # This should really be done as a build-time hook, if that were possible.
        assert (
            self.relation_name in self.charm.meta.requires
        ), "IngressRequirer must be used on a 'requires' relation"
        rel_meta = self.charm.meta.relations[self.relation_name]
        assert (
            rel_meta.interface_name == "ingress"
        ), "IngressRequirer must be used on an 'ingress' relation'"
        assert rel_meta.limit == 1, "IngressRequirer must be used on a 'limit: 1' relation"


class RequestFailed(Exception):
    def __init__(self, status):
        super().__init__(status.message)
        self.status = status


class MockIngressProvider:
    """Class to help with unit testing ingress client charms."""

    app_name = "mock-ingress-provider"

    def __init__(self, harness, relation_name=DEFAULT_RELATION_NAME):
        self.harness = harness
        self.relation_name = relation_name
        self.relation_id = None
        if harness.model.name is None:
            harness._backend.model_name = "test-model"

    def _clear_caches(self):
        for attr in dir(self.harness.charm):
            value = getattr(self.harness.charm, attr)
            if isinstance(value, IngressRequirer) and value.relation_name == self.relation_name:
                try:
                    del value.status
                    del value._data
                    del value._sdi
                except AttributeError:
                    # this happens if a value hasn't been cached yet
                    pass
                break

    def relate(self):
        self.relation_id = self.harness.add_relation(self.relation_name, self.app_name)
        self._clear_caches()
        self.harness.add_relation_unit(self.relation_id, f"{self.app_name}/0")
        self.harness.update_relation_data(
            self.relation_id,
            self.app_name,
            {"_supported_versions": "[v3]"},
        )

    def respond(self):
        request_data = self.harness.get_relation_data(self.relation_id, self.harness.charm.app.name)
        if not request_data.get("data"):
            return
        request = yaml.safe_load(request_data["data"])
        prefix = request["prefix"]
        if prefix.endswith("/"):
            tail = "/"
            prefix = prefix[:-1]
        else:
            tail = ""
        url_base = f"http://{self.app_name}"
        response_data = {"url": f"{url_base}{prefix}{tail}"}
        if request["per_unit_routes"]:
            response_data["unit_urls"] = {
                self.harness.charm.unit.name: f"{url_base}{prefix}-unit-0{tail}"
            }
        self._clear_caches()
        self.harness.update_relation_data(
            self.relation_id,
            self.app_name,
            {"data": yaml.safe_dump(response_data)},
        )


if __name__ == "__main__":
    SCHEMA_FILE.write_text(yaml.safe_dump(get_schema(SCHEMA_URL)))
