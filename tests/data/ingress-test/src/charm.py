#!/usr/bin/env python3


import json
import logging

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, MaintenanceStatus, ModelError
from charms.istio_pilot.v0.ingress import IngressRequirer
from charms.observability_libs.v0.kubernetes_service_patch import KubernetesServicePatch

logger = logging.getLogger(__name__)


class IngressTestCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.service_patcher = KubernetesServicePatch(self, [(self.app.name, 80)])
        self.ingress = IngressRequirer(self)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.httpbin_pebble_ready, self._on_httpbin_pebble_ready)
        self.framework.observe(self.ingress.on.available, self._on_ingress_available)
        self.framework.observe(self.ingress.on.ready, self._on_ingress_ready)
        self.framework.observe(self.ingress.on.failed, self._on_ingress_failed)
        self.framework.observe(self.ingress.on.removed, self._on_ingress_removed)
        self.framework.observe(self.on.get_urls_action, self._on_get_urls_action)

    @property
    def is_running(self):
        try:
            container = self.unit.get_container("httpbin")
            return container.can_connect() and container.get_service("httpbin").is_running()
        except ModelError:
            return False

    def _on_install(self, event):
        if not self.is_running:
            self.unit.status = WaitingStatus("Waiting to start service")

    def _on_httpbin_pebble_ready(self, event):
        self.unit.status = MaintenanceStatus("Starting service")
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
        if self.ingress.is_available:
            # This would've been skipped if it came in before Pebble was ready.
            self.ingress.on.available.emit()
        else:
            self.unit.status = self.ingress.status or ActiveStatus()

    def _on_ingress_available(self, event):
        if not self.is_running:
            # Don't request ingress until we are up and running.
            return
        self.unit.status = MaintenanceStatus("Requesting ingress")
        self.ingress.request(port=80, per_unit_routes=True)
        self.unit.status = WaitingStatus("Waiting for ingress URLs")

    def _on_ingress_ready(self, event):
        self.unit.status = ActiveStatus()

    def _on_ingress_failed(self, event):
        self.unit.status = self.ingress.status

    def _on_ingress_removed(self, event):
        self.unit.status = self.ingress.status

    def _on_get_urls_action(self, action):
        if not self.ingress.url:
            action.fail("Ingress URLs not available")
            return
        action.set_results({
            "url": self.ingress.url,
            "unit-urls": json.dumps(self.ingress.unit_urls),
        })


if __name__ == "__main__":
    main(IngressTestCharm)
