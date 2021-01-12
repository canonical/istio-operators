import os
from pathlib import Path

import yaml

from charmhelpers.core import hookenv
from charms import layer
from charms.reactive import clear_flag, endpoint_from_name, hook, set_flag, when, when_any, when_not
from jinja2 import Environment, FileSystemLoader


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
    http.configure(hostname=hostname, port=hookenv.config()['xds-ca-tls-port'])


@when_any(
    "layer.docker-resource.oci-image.changed", "config.changed", "endpoint.service-mesh.changed"
)
def update_image():
    clear_flag("charm.started")
    clear_flag('layer.docker-resource.oci-image.changed')
    clear_flag('config.changed')
    clear_flag('endpoint.service-mesh.changed')


@when("layer.docker-resource.oci-image.available")
@when_not("charm.started")
def start_charm():
    layer.status.maintenance("configuring container")

    namespace = os.environ["JUJU_MODEL_NAME"]
    config = dict(hookenv.config())

    service_mesh = endpoint_from_name('service-mesh')
    routes = service_mesh.routes()

    try:
        auth_route = next(r for r in routes if r['auth'])
    except StopIteration:
        auth_route = None

    # See https://bugs.launchpad.net/juju/+bug/1900475 for why this isn't inlined below
    if routes:
        custom_resources = {
            'gateways.networking.istio.io': [
                {
                    'apiVersion': 'networking.istio.io/v1beta1',
                    'kind': 'Gateway',
                    'metadata': {
                        'name': config['default-gateway'],
                    },
                    'spec': {
                        'selector': {'istio': 'ingressgateway'},
                        'servers': [
                            {
                                'hosts': ['*'],
                                'port': {'name': 'http', 'number': 80, 'protocol': 'HTTP'},
                            }
                        ],
                    },
                }
            ],
            'virtualservices.networking.istio.io': [
                {
                    'apiVersion': 'networking.istio.io/v1alpha3',
                    'kind': 'VirtualService',
                    'metadata': {'name': route['service']},
                    'spec': {
                        'gateways': [f'{namespace}/{config["default-gateway"]}'],
                        'hosts': ['*'],
                        'http': [
                            {
                                'match': [{'uri': {'prefix': route['prefix']}}],
                                'rewrite': {'uri': route['rewrite']},
                                'route': [
                                    {
                                        'destination': {
                                            'host': f'{route["service"]}.{namespace}.svc.cluster.local',
                                            'port': {'number': route['port']},
                                        }
                                    }
                                ],
                            }
                        ],
                    },
                }
                for route in service_mesh.routes()
            ],
        }
    else:
        custom_resources = {}

    if auth_route:
        request_headers = [{'exact': h} for h in auth_route['auth']['request_headers']]
        response_headers = [{'exact': h} for h in auth_route['auth']['response_headers']]
        custom_resources['rbacconfigs.rbac.istio.io'] = [
            {
                'apiVersion': 'rbac.istio.io/v1alpha1',
                'kind': 'RbacConfig',
                'metadata': {'name': 'default'},
                'spec': {'mode': 'OFF'},
            }
        ]
        custom_resources['envoyfilters.networking.istio.io'] = [
            {
                'apiVersion': 'networking.istio.io/v1alpha3',
                'kind': 'EnvoyFilter',
                'metadata': {'name': 'authn-filter'},
                'spec': {
                    'filters': [
                        {
                            'filterConfig': {
                                'httpService': {
                                    'authorizationRequest': {
                                        'allowedHeaders': {
                                            'patterns': request_headers,
                                        }
                                    },
                                    'authorizationResponse': {
                                        'allowedUpstreamHeaders': {
                                            'patterns': response_headers,
                                        },
                                    },
                                    'serverUri': {
                                        'cluster': f'outbound|{auth_route["port"]}||{auth_route["service"]}.{namespace}.svc.cluster.local',
                                        'failureModeAllow': False,
                                        'timeout': '10s',
                                        'uri': f'http://{auth_route["service"]}.{namespace}.svc.cluster.local:{auth_route["port"]}',
                                    },
                                }
                            },
                            'filterName': 'envoy.ext_authz',
                            'filterType': 'HTTP',
                            'insertPosition': {'index': 'FIRST'},
                            'listenerMatch': {'listenerType': 'GATEWAY'},
                        }
                    ],
                    'workloadLabels': {
                        'istio': 'ingressgateway',
                    },
                },
            }
        ]

    image = layer.docker_resource.get_info("oci-image")
    tconfig = {k.replace('-', '_'): v for k, v in config.items()}
    tconfig['service_name'] = hookenv.service_name()
    tconfig['namespace'] = namespace
    env = Environment(
        loader=FileSystemLoader('templates'), variable_start_string='[[', variable_end_string=']]'
    )

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
                            "field": {"path": "spec.serviceAccountName", "api-version": "v1"}
                        },
                        "PILOT_TRACE_SAMPLING": "1",
                        "CONFIG_NAMESPACE": "istio-config",
                        "PILOT_ENABLE_PROTOCOL_SNIFFING_FOR_OUTBOUND": "true",
                        "PILOT_ENABLE_PROTOCOL_SNIFFING_FOR_INBOUND": "false",
                        "INJECTION_WEBHOOK_CONFIG_NAME": f"{namespace}-sidecar-injector",
                        "ISTIOD_ADDR": f"{hookenv.service_name()}.{namespace}.svc:{config['xds-ca-tls-port']}",
                        "PILOT_EXTERNAL_GALLEY": "false",
                    },
                    "ports": [
                        {"name": "debug", "containerPort": config['debug-port']},
                        {"name": "grpc-xds", "containerPort": config['xds-ca-port']},
                        {"name": "xds", "containerPort": config["xds-ca-tls-port"]},
                        {"name": "webhook", "containerPort": config['webhook-port']},
                    ],
                    "kubernetes": {
                        "readinessProbe": {
                            "failureThreshold": 3,
                            "httpGet": {
                                "path": "/ready",
                                "port": config['debug-port'],
                                "scheme": "HTTP",
                            },
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
                                {
                                    "path": "mesh",
                                    "content": env.get_template('mesh').render(tconfig),
                                },
                                {
                                    "path": "meshNetworks",
                                    "content": env.get_template('meshNetworks').render(tconfig),
                                },
                                {
                                    "path": "values.yaml",
                                    "content": env.get_template('values.yaml').render(tconfig),
                                },
                            ],
                        },
                        {
                            "name": "local-certs",
                            "mountPath": "/var/run/secrets/istio-dns",
                            "emptyDir": {"medium": "Memory"},
                        },
                        {
                            "name": "inject",
                            "mountPath": "/var/lib/istio/inject",
                            "files": [
                                {
                                    "path": "config",
                                    "content": env.get_template('config').render(tconfig),
                                },
                                {
                                    "path": "values",
                                    "content": env.get_template('values').render(tconfig),
                                },
                            ],
                        },
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
                'customResources': custom_resources,
                "mutatingWebhookConfigurations": [
                    {
                        "name": "sidecar-injector",
                        "webhooks": [
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
                                "namespaceSelector": {"matchLabels": {"juju-model": namespace}},
                                "objectSelector": {
                                    "matchExpressions": [
                                        {
                                            'key': 'juju-app',
                                            'operator': 'In',
                                            'values': ['nonexistent']
                                            + [route['service'] for route in service_mesh.routes()],
                                        },
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        },
    )

    layer.status.maintenance("creating container")
    set_flag("charm.started")
