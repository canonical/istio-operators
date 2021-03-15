#!/usr/bin/env python3

import logging
from pathlib import Path
import yaml

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus
from jinja2 import Environment, FileSystemLoader

from oci_image import OCIImageResource, OCIImageResourceError
from k8s_service import ProvideK8sService
from interface_service_mesh import ServiceMeshProvides


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return
        self.log = logging.getLogger(__name__)

        ProvideK8sService(
            self,
            "istio-pilot",
            service_name=f"{self.app.name}.{self.model.name}.svc",
            service_port=self.model.config["xds-ca-tls-port"],
        )
        self.service_mesh = ServiceMeshProvides(self, "service-mesh")

        self.image = OCIImageResource(self, "oci-image")
        for event in [
            self.on.install,
            self.on.leader_elected,
            self.on.upgrade_charm,
            self.on.config_changed,
            self.on.service_mesh_relation_changed,
        ]:
            self.framework.observe(event, self.main)

    def main(self, event):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            self.log.info(e)
            return

        self.model.unit.status = MaintenanceStatus("Setting pod spec")

        config = self.model.config
        namespace = self.model.name
        service_name = self.model.app.name

        routes = self.service_mesh.routes()

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
                    for route in self.service_mesh.routes()
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

        tconfig = {k.replace('-', '_'): v for k, v in config.items()}
        tconfig['service_name'] = service_name
        tconfig['namespace'] = namespace
        env = Environment(
            loader=FileSystemLoader('templates'),
            variable_start_string='[[',
            variable_end_string=']]',
        )

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
                        "name": "discovery",
                        "args": [
                            "discovery",
                            f"--monitoringAddr={config.get('monitoring-address')}",
                            "--log_output_level=all:debug",
                            "--domain",
                            "cluster.local",
                            f"--secureGrpcAddr={config.get('secure-grpc-address')}",
                            "--trust-domain=cluster.local",
                            "--keepaliveMaxServerConnectionAge",
                            "30m",
                            "--disable-install-crds=true",
                        ],
                        "imageDetails": image_details,
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
                            "ISTIOD_ADDR": f"{service_name}.{namespace}.svc:{config.get('xds-ca-tls-port')}",
                            "PILOT_EXTERNAL_GALLEY": "false",
                        },
                        "ports": [
                            {"name": "debug", "containerPort": config.get('debug-port')},
                            {"name": "grpc-xds", "containerPort": config.get('xds-ca-port')},
                            {"name": "xds", "containerPort": config.get("xds-ca-tls-port")},
                            {"name": "webhook", "containerPort": config.get('webhook-port')},
                        ],
                        "kubernetes": {
                            "readinessProbe": {
                                "failureThreshold": 3,
                                "httpGet": {
                                    "path": "/ready",
                                    "port": config.get('debug-port'),
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
                                            "name": service_name,
                                            "namespace": namespace,
                                            "path": "/inject",
                                            "port": config.get('webhook-port'),
                                        },
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
                                                + [
                                                    route['service']
                                                    for route in self.service_mesh.routes()
                                                ],
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

        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(Operator)
