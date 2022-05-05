import logging
from ops.framework import Object
from lightkube.generic_resource import create_global_resource

DEFAULT_RELATION_NAME = "gateway"


class GatewayProvider(Object):
    def __init__(self, charm, lightkube_client, resource_handler):
        super().__init__(charm, DEFAULT_RELATION_NAME)
        self.lightkube_client = lightkube_client
        self.log = logging.getLogger(__name__)
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
