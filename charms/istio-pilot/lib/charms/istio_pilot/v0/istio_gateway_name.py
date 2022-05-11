import logging
from ops.framework import Object
from ops.model import Application

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


DEFAULT_RELATION_NAME = "gateway"
DEFAULT_INTERFACE_NAME = "istio-gateway-name"

logger = logging.getLogger(__name__)


class GatewayRelationError(Exception):
    pass


class GatewayRelationMissingError(GatewayRelationError):
    def __init__(self):
        self.message = "Missing gateway relation with istio-pilot"
        super().__init__(self.message)


class GatewayRelationTooManyError(GatewayRelationError):
    def __init__(self):
        self.message = "Too many istio-gateway-name relations"
        super().__init__(self.message)


class GatewayRelationDataMissingError(GatewayRelationError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class GatewayRequirer(Object):
    def __init__(self, charm, relation_name: str = DEFAULT_RELATION_NAME):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name

    def get_relation_data(self):
        if not self.model.unit.is_leader():
            return
        gateway = self.model.relations[self.relation_name]
        if len(gateway) == 0:
            raise GatewayRelationMissingError()
        if len(gateway) > 1:
            raise GatewayRelationTooManyError()

        remote_app = [
            app
            for app in gateway[0].data.keys()
            if isinstance(app, Application) and not app._is_our_app
        ][0]

        data = gateway[0].data[remote_app]

        if not "gateway_name" in data:
            logger.error(
                "Missing gateway name in gateway relation data. Waiting for gateway creation in istio-pilot"
            )
            raise GatewayRelationDataMissingError(
                "Missing gateway name in gateway relation data. Waiting for gateway creation in istio-pilot"
            )

        if not "gateway_namespace" in data:
            logger.error("Missing gateway namespace in gateway relation data")
            raise GatewayRelationDataMissingError(
                "Missing gateway namespace in gateway relation data"
            )

        return {
            "gateway_name": data["gateway_name"],
            "gateway_namespace": data["gateway_namespace"],
        }


class GatewayProvider(Object):
    def __init__(self, charm, lightkube_client, resource_handler):
        super().__init__(charm, DEFAULT_RELATION_NAME)
        self.lightkube_client = lightkube_client
        self.charm = charm
        self.resource_handler = resource_handler
        self.framework.observe(
            charm.on[DEFAULT_RELATION_NAME].relation_changed, self._on_gateway_relation_changed
        )
        self.framework.observe(charm.on.config_changed, self._on_gateway_config_changed)
        self.framework.observe(charm.on.update_status, self._on_gateway_config_changed)

    def _validate_gateway_exists(self):
        response = self.lightkube_client.get(
            self.resource_handler.get_custom_resource_class_from_filename(
                filename='gateway.yaml.j2'
            ),
            self.model.config['default-gateway'],
            namespace=self.model.name,
        )
        return True if response else False

    def _on_gateway_relation_changed(self, event):
        if self.model.unit.is_leader():
            relations = self.model.relations["gateway"]
            for relation in relations:
                relation.data[self.charm.app].update(
                    {
                        "gateway_name": self.model.config["default-gateway"]
                        if self._validate_gateway_exists()
                        else "",
                        "gateway_namespace": self.model.name,
                    }
                )

    def _on_gateway_config_changed(self, event):
        if len(self.model.relations[DEFAULT_RELATION_NAME]) > 0:
            self._on_gateway_relation_changed(event)
