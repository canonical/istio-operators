import logging

from ops.framework import Object

logger = logging.getLogger(__name__)

RELATION_NAME = "gateway-info"


class GatewayProvider(Object):
    def __init__(self, charm, relation_name=RELATION_NAME):
        super().__init__(charm, relation_name)

    def send_gateway_relation_data(self, charm, gateway_name, gateway_namespace):
        relations = self.model.relations["gateway-info"]
        for relation in relations:
            relation.data[charm].update(
                {
                    "gateway_name": gateway_name,
                    "gateway_namespace": gateway_namespace,
                }
            )
