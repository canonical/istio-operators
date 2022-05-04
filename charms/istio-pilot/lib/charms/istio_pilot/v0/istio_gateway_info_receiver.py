import logging
from ops.framework import EventBase, EventSource, Object
from ops.charm import CharmEvents, RelationChangedEvent

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


DEFAULT_RELATION_INTERFACE = "gateway"


class GatewayRelationUpdatedEvent(EventBase):
    pass


class GatewayRelationEvents(CharmEvents):
    gateway_relation_updated = EventSource(GatewayRelationUpdatedEvent)


class GatewayConsumer(Object):
    def __init__(self, charm, _stored, relation_name: str = DEFAULT_RELATION_INTERFACE):
        super().__init__(charm, relation_name)
        self._stored = _stored
        self.charm = charm
        self.log = logging.getLogger(__name__)
        self.relation_name = relation_name
        self._stored.gateway_info = {}

        self.framework.observe(charm.on[relation_name].relation_changed, self._on_relation_changed)

    def _on_relation_changed(self, event: RelationChangedEvent):
        if not self.model.unit.is_leader():
            return

        rel_data = event.relation.data[event.app]
        self.log.debug(f"istio_gateway_info_receiver.py ~ gateway relation databag {rel_data}")

        if not "gateway_name" in rel_data:
            self.log.error("Missing gateway_name in relation data")

        if not "gateway_namespace" in rel_data:
            self.log.error("Missing gateway_name or gateway_namespace in relation data")

        self._stored.gateway_info = {
            "gateway_name": rel_data.get("gateway_name"),
            "gateway_namespace": rel_data.get("gateway_namespace"),
        }

    def get_gateway_info(self):
        gateway_data = self._stored.gateway_info
        if not gateway_data.get("gateway_name"):
            raise Exception("Missing gateway name. Waiting for gateway creation in istio-pilot")
        if not gateway_data.get("gateway_namespace"):
            raise Exception("Missing gateway namespace")
        return self._stored.gateway_info
