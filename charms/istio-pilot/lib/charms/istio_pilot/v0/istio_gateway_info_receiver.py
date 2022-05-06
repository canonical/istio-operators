import logging
from ops.framework import Object
from ops.model import Application

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


DEFAULT_RELATION_INTERFACE = "gateway"


class GatewayConsumer(Object):
    def __init__(self, charm, relation_name: str = DEFAULT_RELATION_INTERFACE):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.log = logging.getLogger(__name__)
        self.relation_name = relation_name

    def get_relation_data(self):
        if not self.model.unit.is_leader():
            return
        gateway = self.model.relations[self.relation_name]
        if len(gateway) == 0:
            raise Exception("Missing gateway relation with istio-pilot")
        if len(gateway) > 1:
            raise Exception("Too many gateway relations")

        remote_app = [
            app
            for app in gateway[0].data.keys()
            if isinstance(app, Application) and not app._is_our_app
        ][0]

        data = gateway[0].data[remote_app]

        if not "gateway_name" in data:
            self.log.error(
                "Missing gateway name in gateway relation data. Waiting for gateway creation in istio-pilot"
            )
            raise Exception(
                "Missing gateway name in gateway relation data. Waiting for gateway creation in istio-pilot"
            )

        if not "gateway_namespace" in data:
            self.log.error("Missing gateway namespace in gateway relation data")
            raise Exception("Missing gateway namespace in gateway relation data")

        return {
            "gateway_name": data["gateway_name"],
            "gateway_namespace": data["gateway_namespace"],
        }
