import os
import time
from pathlib import Path

from kubernetes import client, config

from charms import layer
from charms.reactive import (
    clear_flag,
    endpoint_from_name,
    hook,
    hookenv,
    set_flag,
    when,
    when_any,
    when_not,
)


@hook("upgrade-charm")
def upgrade_charm():
    clear_flag("charm.started")


@when("charm.started")
def charm_ready():
    layer.status.active("")


@when_any("layer.docker-resource.oci-image.changed")
def update_image():
    clear_flag("charm.started")


@when("layer.docker-resource.oci-image.available", "istio-pilot.available")
@when_not("charm.started")
def start_charm():
    layer.status.maintenance("configuring container")

    image_info = layer.docker_resource.get_info("oci-image")
    namespace = os.environ["JUJU_MODEL_NAME"]
    pilot = endpoint_from_name("istio-pilot").services()[0]
    pilot_service = pilot['hosts'][0]['hostname']
    pilot_port = pilot['hosts'][0]['port']
    cfg = dict(hookenv.config())

    # Talk to the K8s API to read the auto-generated root certificate secret.
    # Borrow the env vars from the root process that let the Kubernetes
    # client automatically look up connection info, since `load_incluster_config`
    # for whatever reason doesn't support loading the serviceaccount token from disk.
    os.environ.update(
        dict(
            e.split("=")
            for e in Path("/proc/1/environ").read_text().split("\x00")
            if "KUBERNETES_SERVICE" in e
        )
    )

    config.load_incluster_config()
    v1 = client.CoreV1Api()
    layer.status.maintenance("Waiting for configmap/istio-ca-root-cert to be created")
    for _ in range(30):
        try:
            config_map = v1.read_namespaced_config_map(
                name="istio-ca-root-cert", namespace=namespace
            )
            if config_map.data.get("root-cert.pem"):
                break
            else:
                hookenv.log("Got empty certificate, waiting for real one")
                time.sleep(10)
        except client.rest.ApiException as err:
            hookenv.log(err)
            hookenv.log(err.status)
            hookenv.log(err.reason)
            hookenv.log(err.body)
            hookenv.log(err.headers)
            time.sleep(10)
    else:
        layer.status.blocked("istio-ca-root-cert certificate not found.")
        return False

    layer.caas_base.pod_spec_set(
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
                        f"{namespace}.svc.cluster.local",
                        "--proxyLogLevel=warning",
                        "--proxyComponentLogLevel=misc:error",
                        "--log_output_level=default:info",
                        "--drainDuration",
                        "45s",
                        "--parentShutdownDuration",
                        "1m0s",
                        "--connectTimeout",
                        "10s",
                        "--serviceCluster",
                        hookenv.service_name(),
                        # "--zipkinAddress",
                        # "zipkin.istio-system:9411",
                        "--proxyAdminPort",
                        cfg['proxy-admin-port'],
                        "--statusPort",
                        cfg['status-port'],
                        "--controlPlaneAuthPolicy",
                        "NONE",
                        "--discoveryAddress",
                        f"{pilot_service}:{pilot_port}",
                        "--trust-domain=cluster.local",
                    ],
                    "imageDetails": {
                        "imagePath": image_info.registry_path,
                        "username": image_info.username,
                        "password": image_info.password,
                    },
                    "envConfig": {
                        "JWT_POLICY": "first-party-jwt",
                        "PILOT_CERT_PROVIDER": "istiod",
                        "ISTIO_META_USER_SDS": "true",
                        "CA_ADDR": f"{pilot_service}:{pilot_port}",
                        "NODE_NAME": {
                            "field": {"path": "spec.nodeName", "api-version": "v1"}
                        },
                        "POD_NAME": {
                            "field": {"path": "metadata.name", "api-version": "v1"}
                        },
                        "POD_NAMESPACE": namespace,
                        "INSTANCE_IP": {
                            "field": {"path": "status.podIP", "api-version": "v1"}
                        },
                        "HOST_IP": {
                            "field": {"path": "status.hostIP", "api-version": "v1"}
                        },
                        "SERVICE_ACCOUNT": {
                            "field": {
                                "path": "spec.serviceAccountName",
                                "api-version": "v1",
                            }
                        },
                        "ISTIO_META_WORKLOAD_NAME": hookenv.service_name(),
                        "ISTIO_META_OWNER": f"kubernetes://api/apps/v1/namespaces/{namespace}/deployments/{hookenv.service_name()}",
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
                        {"name": "status-port", "containerPort": 15020},
                        {"name": "http2", "containerPort": 80},
                        {"name": "https", "containerPort": 443},
                        {"name": "kiali", "containerPort": 15029},
                        {"name": "prometheus", "containerPort": 15030},
                        {"name": "grafana", "containerPort": 15031},
                        {"name": "tracing", "containerPort": 15032},
                        {"name": "tls", "containerPort": 15443},
                        {"name": "unknown1", "containerPort": 15011},
                        {"name": "unknown2", "containerPort": 8060},
                        {"name": "unknown3", "containerPort": 853},
                    ],
                    "kubernetes": {
                        "readinessProbe": {
                            "failureThreshold": 30,
                            "httpGet": {
                                "path": "/healthz/ready",
                                "port": 15020,
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
                                    "content": "\n".join(
                                        [
                                            'app="istio-ingressgateway"',
                                            'istio="ingressgateway"',
                                        ]
                                    ),
                                },
                            ],
                        },
                    ],
                }
            ],
        }
    )

    layer.status.maintenance("creating container")
    set_flag("charm.started")
