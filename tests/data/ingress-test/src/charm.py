#!/usr/bin/env python3


import logging

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus
from charms.istio_pilot.v0.ingress import IngressRequirer, RequestFailed
from charms.observability_libs.v0.kubernetes_service_patch import KubernetesServicePatch

logger = logging.getLogger(__name__)


class IngressTestCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.service_patcher = KubernetesServicePatch(self, [(self.app.name, 80)])
        self.ingress = IngressRequirer(self, "ingress")

        self.framework.observe(self.on.httpbin_pebble_ready, self._on_httpbin_pebble_ready)
        if self.unit.is_leader():
            self.framework.observe(self.on.ingress_relation_created, self._on_ingress)
            self.framework.observe(self.on.ingress_relation_changed, self._on_ingress)
            self.framework.observe(self.on.leader_elected, self._on_ingress)
        self.framework.observe(self.on.get_urls_action, self._on_get_urls_action)

    def _on_httpbin_pebble_ready(self, event):
        container = event.workload
        pebble_layer = {
            "summary": "httpbin layer",
            "description": "pebble config layer for httpbin",
            "services": {
                "httpbin": {
                    "override": "replace",
                    "summary": "httpbin",
                    "command": "gunicorn -b 0.0.0.0:80 httpbin:app -k gevent",
                    "startup": "enabled",
                    "environment": {"thing": "ingress-test"},
                }
            },
        }
        container.add_layer("httpbin", pebble_layer, combine=True)
        container.autostart()

    def _on_ingress(self, _):
        try:
            self.ingress.request(port=80, per_unit_routes=True)
        except RequestFailed as e:
            self.unit.status = e.status
        else:
            self.unit.status = ActiveStatus()

    def _on_get_urls_action(self, action):
        if not self.unit.is_leader():
            # https://github.com/canonical/serialized-data-interface/issues/27
            action.fail("Must be called on leader")
            return
        if not self.ingress.url:
            action.fail("Ingress not available")
            return
        action.set_results({
            "url": self.ingress.url,
            "unit-urls": self.ingress.unit_urls,
        })


if __name__ == "__main__":
    main(IngressTestCharm)
