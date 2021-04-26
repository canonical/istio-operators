#!/usr/bin/env python3

import logging
import os
from pathlib import Path

from kubernetes import client, config
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces

from oci_image import OCIImageResource, OCIImageResourceError


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            self.model.unit.status = WaitingStatus(str(err))
            return
        except NoCompatibleVersions as err:
            self.model.unit.status = BlockedStatus(str(err))
            return
        else:
            self.model.unit.status = ActiveStatus()

        self.log = logging.getLogger(__name__)
        self.image = OCIImageResource(self, "oci-image")
        for event in [
            self.on.install,
            self.on.leader_elected,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.config_changed,
            self.on['istio-pilot'].relation_changed,
        ]:
            self.framework.observe(event, self.main)

    def main(self, event):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            self.log.info(e)
            return

        if not ((pilot := self.interfaces["istio-pilot"]) and pilot.get_data()):
            self.model.unit.status = WaitingStatus("Waiting for istio-pilot relation data")
            return
        pilot = list(pilot.get_data().values())[0]
        pilot_url = "{service-name}:{service-port}".format(**pilot)

        self.model.unit.status = MaintenanceStatus("Setting pod spec")

        cfg = self.model.config
        namespace = self.model.name
        service_name = self.model.app.name

        config_map = self.check_ca_root_cert(namespace)

        if not config_map:
            self.model.unit.status = WaitingStatus("Waiting for Istio Pilot information")
            return

        self.model.pod.set_spec(
            {
                "version": 3,
                "serviceAccount": {
                    "roles": [
                        {
                            "global": True,
                            "rules": [
                                {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]},
                                {"nonResourceURLs": ["*"], "verbs": ["*"]},
                            ],
                        }
                    ]
                },
                "containers": [
                    {
                        "name": "istio-proxy",
                        "args": [
                            "proxy",
                            "router",
                            "--domain",
                            f"{self.model.name}.svc.cluster.local",
                            "--proxyLogLevel=warning",
                            "--proxyComponentLogLevel=misc:error",
                            f"--log_output_level={cfg['log-level']}",
                            "--drainDuration",
                            "45s",
                            "--parentShutdownDuration",
                            "1m0s",
                            "--connectTimeout",
                            "10s",
                            "--serviceCluster",
                            service_name,
                            "--proxyAdminPort",
                            cfg['proxy-admin-port'],
                            "--statusPort",
                            str(cfg['status-port']),
                            "--controlPlaneAuthPolicy",
                            "NONE",
                            "--discoveryAddress",
                            pilot_url,
                            "--trust-domain=cluster.local",
                        ],
                        "imageDetails": image_details,
                        "envConfig": {
                            "JWT_POLICY": "first-party-jwt",
                            "PILOT_CERT_PROVIDER": "istiod",
                            "ISTIO_META_USER_SDS": "true",
                            "CA_ADDR": pilot_url,
                            "NODE_NAME": {"field": {"path": "spec.nodeName", "api-version": "v1"}},
                            "POD_NAME": {"field": {"path": "metadata.name", "api-version": "v1"}},
                            "POD_NAMESPACE": service_name,
                            "INSTANCE_IP": {"field": {"path": "status.podIP", "api-version": "v1"}},
                            "HOST_IP": {"field": {"path": "status.hostIP", "api-version": "v1"}},
                            "SERVICE_ACCOUNT": {
                                "field": {"path": "spec.serviceAccountName", "api-version": "v1"}
                            },
                            "ISTIO_META_WORKLOAD_NAME": service_name,
                            "ISTIO_META_OWNER": f"kubernetes://api/apps/v1/namespaces/{namespace}/deployments/{service_name}",
                            "ISTIO_META_MESH_ID": "cluster.local",
                            "ISTIO_AUTO_MTLS_ENABLED": "true",
                            "ISTIO_META_POD_NAME": {
                                "field": {"path": "metadata.name", "api-version": "v1"}
                            },
                            "ISTIO_META_CONFIG_NAMESPACE": namespace,
                            "ISTIO_META_ROUTER_MODE": "sni-dnat",
                            "ISTIO_META_CLUSTER_ID": "Kubernetes",
                        },
                        "ports": [
                            {"name": "status-port", "containerPort": cfg['status-port']},
                            {"name": "http2", "containerPort": cfg['http-port']},
                            {"name": "https", "containerPort": cfg['https-port']},
                            {"name": "kiali", "containerPort": cfg['kiali-port']},
                            {"name": "prometheus", "containerPort": cfg['prometheus-port']},
                            {"name": "grafana", "containerPort": cfg['grafana-port']},
                            {"name": "tracing", "containerPort": cfg['tracing-port']},
                            {"name": "tls", "containerPort": cfg['tls-port']},
                            {"name": "pilot", "containerPort": cfg['xds-ca-port-legacy']},
                            {"name": "citadel", "containerPort": cfg['citadel-grpc-port']},
                            {"name": "dns-tls", "containerPort": cfg['dns-tls-port']},
                        ],
                        "kubernetes": {
                            "readinessProbe": {
                                "failureThreshold": 30,
                                "httpGet": {
                                    "path": "/healthz/ready",
                                    "port": cfg['status-port'],
                                    "scheme": "HTTP",
                                },
                                "initialDelaySeconds": 1,
                                "periodSeconds": 2,
                                "successThreshold": 1,
                                "timeoutSeconds": 1,
                            },
                        },
                        "volumeConfig": [
                            {
                                "name": "istiod-ca-cert",
                                "mountPath": "/var/run/secrets/istio",
                                "files": [
                                    {
                                        "path": "root-cert.pem",
                                        "content": config_map.data["root-cert.pem"],
                                    }
                                ],
                            },
                            {
                                "name": "ingressgatewaysdsudspath",
                                "mountPath": "/var/run/ingress_gateway",
                                "emptyDir": {"medium": "Memory"},
                            },
                            {
                                "name": "podinfo",
                                "mountPath": "/etc/istio/pod",
                                "files": [
                                    {
                                        "path": "annotations",
                                        "content": 'sidecar.istio.io/inject="false"',
                                    },
                                    {
                                        "path": "labels",
                                        "content": 'app="istio-ingressgateway"\nistio="ingressgateway"',
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        )

        self.model.unit.status = ActiveStatus()

    def check_ca_root_cert(self, namespace):
        # Workaround due to this bug: https://bugs.launchpad.net/juju/+bug/1892255
        os.environ.update(
            dict(
                e.split("=")
                for e in Path("/proc/1/environ").read_text().split("\x00")
                if "KUBERNETES_SERVICE" in e
            )
        )

        config.load_incluster_config()
        v1 = client.CoreV1Api()
        self.model.unit.status = MaintenanceStatus(
            "Waiting for configmap/istio-ca-root-cert to be created"
        )
        try:
            config_map = v1.read_namespaced_config_map(
                name="istio-ca-root-cert", namespace=namespace
            )
            if not config_map.data.get("root-cert.pem"):
                self.log.info("Got empty certificate, waiting for real one")
                return None
        except client.rest.ApiException as err:
            self.log.info(err)
            self.model.unit.status = BlockedStatus("istio-ca-root-cert certificate not found.")
            return None
        return config_map


if __name__ == "__main__":
    main(Operator)
