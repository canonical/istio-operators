import os
from pathlib import Path

import yaml

from charmhelpers.core import hookenv
from charms import layer
from charms.reactive import clear_flag, hook, set_flag, when, when_any, when_not


@hook("upgrade-charm")
def upgrade_charm():
    clear_flag("charm.started")


@when("charm.started")
def charm_ready():
    layer.status.active("")


@when("istio-pilot.available")
def configure_http(http):
    namespace = os.environ["JUJU_MODEL_NAME"]
    hostname = f"{hookenv.service_name()}.{namespace}.svc"
    http.configure(hostname=hostname, port=hookenv.config()['xds-ca-port'])


@when_any("layer.docker-resource.oci-image.changed")
def update_image():
    clear_flag("charm.started")


@when("layer.docker-resource.oci-image.available")
@when_not("charm.started")
def start_charm():
    layer.status.maintenance("configuring container")

    image = layer.docker_resource.get_info("oci-image")
    namespace = os.environ["JUJU_MODEL_NAME"]
    config = dict(hookenv.config())

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
                    "name": "discovery",
                    "args": [
                        "discovery",
                        f"--monitoringAddr={config['monitoring-address']}",
                        "--log_output_level=all:debug",
                        "--domain",
                        "cluster.local",
                        f"--secureGrpcAddr={config['secure-grpc-address']}",
                        "--trust-domain=cluster.local",
                        "--keepaliveMaxServerConnectionAge",
                        "30m",
                        "--disable-install-crds=true",
                    ],
                    "imageDetails": {
                        "imagePath": image.registry_path,
                        "username": image.username,
                        "password": image.password,
                    },
                    "envConfig": {
                        "JWT_POLICY": "first-party-jwt",
                        "PILOT_CERT_PROVIDER": "istiod",
                        "POD_NAME": {"field": {"path": "metadata.name", "api-version": "v1"}},
                        "POD_NAMESPACE": namespace,
                        "SERVICE_ACCOUNT": {
                            "field": {"path": "spec.serviceAccountName", "api-version": "v1",}
                        },
                        "PILOT_TRACE_SAMPLING": "1",
                        "CONFIG_NAMESPACE": "istio-config",
                        "PILOT_ENABLE_PROTOCOL_SNIFFING_FOR_OUTBOUND": "true",
                        "PILOT_ENABLE_PROTOCOL_SNIFFING_FOR_INBOUND": "false",
                        "INJECTION_WEBHOOK_CONFIG_NAME": f"{namespace}-sidecar-injector",
                        "ISTIOD_ADDR": f"{hookenv.service_name()}.{namespace}.svc:{config['xds-ca-port']}",
                        "PILOT_EXTERNAL_GALLEY": "false",
                    },
                    "ports": [
                        {"name": "debug", "containerPort": 8080},
                        {"name": "grpc-xds", "containerPort": 15010},
                        {"name": "xds", "containerPort": config["xds-ca-port"]},
                        {"name": "webhook", "containerPort": config['webhook-port']},
                    ],
                    "kubernetes": {
                        "readinessProbe": {
                            "failureThreshold": 3,
                            "httpGet": {"path": "/ready", "port": 8080, "scheme": "HTTP",},
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5,
                            "successThreshold": 1,
                            "timeoutSeconds": 5,
                        },
                    },
                    "volumeConfig": [
                        {
                            "name": "config-volume",
                            "mountPath": "/etc/istio/config",
                            "files": [
                                {"path": "mesh", "content": Path("files/mesh").read_text(),},
                                {
                                    "path": "meshNetworks",
                                    "content": Path("files/meshNetworks").read_text(),
                                },
                                {
                                    "path": "values.yaml",
                                    "content": Path("files/values.yaml").read_text(),
                                },
                            ],
                        },
                        {
                            "name": "local-certs",
                            "mountPath": "/var/run/secrets/istio-dns",
                            "emptyDir": {"medium": "Memory"},
                        },
                        #  {
                        #      "name": "cacerts",
                        #      "mountPath": "/etc/cacerts",
                        #  },
                        {
                            "name": "inject",
                            "mountPath": "/var/lib/istio/inject",
                            "files": [
                                {"path": "config", "content": Path("files/config").read_text(),},
                                {"path": "values", "content": Path("files/values").read_text(),},
                            ],
                        },
                        #  {
                        #      "name": "istiod",
                        #      "mountPath": "/var/lib/istio/local",
                        #  },
                        #  {
                        #      "name": "istiod-service-account-token-nhc8h",
                        #      "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                        #  },
                    ],
                },
            ],
        },
        k8s_resources={
            "kubernetesResources": {
                "customResourceDefinitions": [
                    {"name": crd["metadata"]["name"], "spec": crd["spec"]}
                    for crd in yaml.safe_load_all(Path("files/crds.yaml").read_text())
                ],
                "mutatingWebhookConfigurations": {
                    "sidecar-injector": [
                        {
                            "name": "sidecar-injector.istio.io",
                            "clientConfig": {
                                "service": {
                                    "name": hookenv.service_name(),
                                    "namespace": namespace,
                                    "path": "/inject",
                                    "port": config['webhook-port'],
                                },
                                #  "caBundle": ca_bundle,
                            },
                            "rules": [
                                {
                                    "operations": ["CREATE"],
                                    "apiGroups": [""],
                                    "apiVersions": ["v1"],
                                    "resources": ["pods"],
                                }
                            ],
                            "failurePolicy": "Fail",
                            "namespaceSelector": {"matchLabels": {"istio-injection": "enabled"}},
                        }
                    ],
                },
            }
        },
    )

    layer.status.maintenance("creating container")
    set_flag("charm.started")
