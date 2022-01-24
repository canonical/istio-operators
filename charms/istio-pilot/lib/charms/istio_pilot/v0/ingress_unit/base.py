import logging
from functools import cached_property
from typing import Any, Dict

import yaml
from ops.charm import CharmBase, RelationEvent
from ops.framework import EventBase, EventSource, Object, ObjectEvents
from ops.model import BlockedStatus, ModelError, Relation, WaitingStatus

log = logging.getLogger(__name__)


class RelationAvailableEvent(RelationEvent):
    """Event triggered when a relation is available (has versions)."""


class RelationReadyEvent(RelationEvent):
    """Event triggered when a relation has the expected remote data."""


class RelationFailedEvent(RelationEvent):
    """Event triggered when something went wrong with a relation."""


class RelationEndpointEvents(ObjectEvents):
    available = EventSource(RelationAvailableEvent)
    ready = EventSource(RelationReadyEvent)
    failed = EventSource(RelationFailedEvent)


class RelationEndpointBase(Object):
    VERSIONS = ["v1"]
    """Versions which are supported by this implementation."""

    ROLE = None
    """Relation role which this class implements. Must be set by subclass."""

    INTERFACE_NAME = None
    """Interface protocol name. Must be set by subclass."""

    SCHEMA_FILE = None
    """Path of schema YAML file. Must be set by subclass."""

    LIMIT = None
    """Limit, if any, for relation endpoint connections."""

    on = RelationEndpointEvents()

    def __init__(self, charm: CharmBase, relation_name: str = None):
        """Constructor for RelationEndpointBase.

        Args:
            charm: The charm that is instantiating the library.
            relation_name: The name of the relation endpoint to bind to
                (defaults to the INTERFACE_NAME).
        """
        if not relation_name:
            relation_name = self.INTERFACE_NAME
        super().__init__(charm, f"relation-{relation_name}")
        self.charm = charm
        self.relation_name = relation_name
        self.schemas = yaml.safe_load(self.SCHEMA_FILE.read_text())
        self._auto_data = None

        self._validate_relation_meta()

        self.framework.observe(
            charm.on[relation_name].relation_created, self._check_relation
        )
        self.framework.observe(
            charm.on[relation_name].relation_created, self._send_versions
        )
        self.framework.observe(
            charm.on[relation_name].relation_created, self._send_auto_data
        )
        self.framework.observe(
            charm.on[relation_name].relation_changed, self._check_relation
        )
        self.framework.observe(
            charm.on[relation_name].relation_changed, self._send_auto_data
        )
        self.framework.observe(charm.on.leader_elected, self._send_versions)
        self.framework.observe(charm.on.leader_elected, self._send_auto_data)
        self.framework.observe(charm.on.upgrade_charm, self._send_versions)
        self.framework.observe(charm.on.upgrade_charm, self._send_auto_data)

    def unwrap(self, relation: Relation):
        """Deserialize the data from the relation.

        Any data which can't be deserialized will be ignored.
        """

    @cached_property
    def relations(self):
        """The list of Relation instances associated with this endpoint."""
        return list(self.charm.model.relations[self.relation_name])

    @cached_property
    def status(self):
        if not self.relations:
            # the key will always exist but may be an empty list
            return BlockedStatus(f"Missing relation: {self.relation_name}")
        status = None
        for relation in self.relations:
            remote_versions = relation.data[relation.app].get("_supported_versions")
            if not remote_versions:
                if relation.units:
                    # If we have remote units but still no version, then there's
                    # probably something wrong and we should be blocked.
                    status = BlockedStatus(
                        f"Missing relation versions: {self.relation_name}"
                    )
                elif not isinstance(status, BlockedStatus):
                    # Otherwise, we might just not have seen the versions yet.
                    status = WaitingStatus(f"Waiting on relation: {self.relation_name}")
            else:
                remote_versions = set(yaml.safe_load(remote_versions))
                if not set.intersection(self.VERSIONS, remote_versions):
                    status = BlockedStatus(
                        f"Incompatible relation versions: {self.relation_name}"
                    )
            except errors.IncompleteRelation:
                if not isinstance(status, BlockedStatus):
                    status = WaitingStatus(f"Waiting on relation: {self.relation_name}")
            except errors.InterfaceSchemaError:
                log.exception(f"Error handling relation: {relation}")
                status = BlockedStatus(f"Error handling relation: {self.relation_name}")
            else:
                return None
        return status

    def is_available(self, relation: Relation = None):
        """Checks whether the given relation, or any relation if not specified,
        is available.

        A given relation is available if the version negotation has succeeded.
        """
        if relation is None:
            relations = self.relations
        else:
            relations = [relation]
        result = False
        for relation in relations:
            try:
                self.schemas.get_version(relation)
            except errors.InterfaceSchemaError:
                # any errors mean this relation isn't available
                pass
            else:
                result = True
        return result

    def is_ready(self, relation: Relation = None):
        """Checks whether the given relation, or any relation if not specified,
        is ready.

        A given relation is ready if the remote side has sent valid data.
        """
        if relation is None:
            relations = self.relations
        else:
            relations = [relation]
        result = False
        for relation in relations:
            try:
                self.schemas.unwrap(relation)
            except errors.InterfaceSchemaError:
                # any errors mean this relation isn't ready
                pass
            else:
                result = True
        return result

    def _check_relation(self, event):
        if self.is_ready:
            self.on.ready.emit()
        elif self.is_available:
            self.on.available.emit()
        elif self.relations and isinstance(self.status, BlockedStatus):
            self.on.failed.emit()

    def _send_versions(self, event):
        if not self.charm.unit.is_leader():
            return
        if isinstance(event, RelationEvent):
            relations = [event.relation]
        else:
            relations = self.relations
        for relation in relations:
            self.schemas.send_versions(relation)

    def _send_auto_data(self, event):
        if not self.charm.unit.is_leader() or not (
            self._auto_app_data or self._auto_unit_data
        ):
            return
        if isinstance(event, RelationEvent):
            relations = [event.relation]
        else:
            relation = self.relations
        for relation in relations:
            if self.is_available(relation):
                self.send_data(relation, self._auto_app_data, self._auto_unit_data)

    def _send_data(
        self,
        relation: Relation,
        *,
        app_data: Dict[str, Any] = None,
        unit_data: Dict[str, Any] = None,
    ):
        """Send data to a given relation.

        Note: only the leader unit can send app data.

        Can raise:
            * ModelError (when not leader)
            * UnversionedRelation (when relation not available)
            * RelationParseError (when relation not available)
            * IncompatibleVersionsError (when relation not available)
            * RelationDataError (when given data is invalid)
        """
        if app_data and not self.charm.unit.is_leader():
            raise ModelError("Only leader can send app-level relation data")
        wrapped = self.schemas.wrap(
            relation,
            {
                self.charm.app: app_data or {},
                self.charm.unit: unit_data or {},
            },
        )
        relation.data.update(wrapped)

    def _validate_relation_meta(self):
        """Validate that the relation is setup properly in the metadata."""
        # This should really be done as a build-time hook, if that were possible.
        cls_name = type(self).__name__
        assert (
            self.relation_name in self.charm.meta.relations
        ), f"Relation {self.relation_name} not found"
        rel_meta = self.charm.meta.relations[self.relation_name]
        assert (
            self.ROLE == rel_meta.role.name
        ), f"{cls_name} must be used on a '{self.ROLE}' relation"
        assert (
            rel_meta.interface_name == self.INTERFACE_NAME
        ), f"{cls_name} must be used on an '{self.INTERFACE_NAME}' relation endpoint"
        if self.LIMIT is not None:
            assert (
                rel_meta.limit == 1
            ), f"{cls_name} must be used on a 'limit: {self.LIMIT}' relation endpoint"


class RelationError(Exception):
    def __init__(self, relation, entity):
        super().__init__(
            "{self.relation.name}:{self.relation.relation_id} from {self.entity.name}"
        )
        self.relation = relation
        self.entity = entity


class UnversionedRelationError(RelationError):
    pass


class InvalidRelationVersionError(RelationError):
    pass


class InvalidRelationDataError(RelationError):
    pass
