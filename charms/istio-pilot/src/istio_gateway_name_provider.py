import logging
from ops.framework import Object, EventBase, EventSource, ObjectEvents
from ops.charm import CharmEvents
from lightkube.core.client import Client
from lightkube.generic_resource import create_namespaced_resource

logger = logging.getLogger(__name__)

DEFAULT_RELATION_NAME = "gateway"


class GatewayProvider(Object):
    def __init__(self, charm, relation_name=DEFAULT_RELATION_NAME):
        super().__init__(charm, relation_name)

    def send_gateway_relation_data(self, charm, gateway_name, gateway_namespace):
        relations = self.model.relations["gateway"]
        for relation in relations:
            relation.data[charm].update(
                {
                    "gateway_name": gateway_name,
                    "gateway_namespace": gateway_namespace,
                }
            )
