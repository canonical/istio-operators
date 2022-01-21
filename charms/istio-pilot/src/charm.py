#!/usr/bin/env python3

import logging
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase, RelationJoinedEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces

from oci_image import OCIImageResource, OCIImageResourceError


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.log = logging.getLogger(__name__)
        self.image = OCIImageResource(self, "oci-image")
        for event in [
            self.on.install,
            self.on.leader_elected,
            self.on.upgrade_charm,
            # self.on.update_status,
            self.on.config_changed,
            self.on['ingress'].relation_changed,
            self.on["istio-pilot"].relation_changed,
            self.on["istio-pilot"].relation_joined,
        ]:
            self.framework.observe(event, self.set_pod_spec)

    def _send_info(self, interfaces):
        if interfaces["istio-pilot"]:
            interfaces["istio-pilot"].send_data(
                {
                    "service-name": f'{self.model.app.name}.{self.model.name}.svc',
                    "service-port": str(self.model.config['xds-ca-tls-port']),
                }
            )
            self.log.info("istio-pilot relation is available info has been sent")
        else:
            self.log.info("istio-pilot relation not available not able to send service info")

    def set_pod_spec(self, event):
        try:
            self._check_leader()

            interfaces = self._get_interfaces()

            image_details = self._check_image_details()

        except CheckFailed as error:
            self.model.unit.status = error.status
            return

        self.model.unit.status = MaintenanceStatus("Setting pod spec")

        self._send_info(interfaces)

        config = self.model.config
        namespace = self.model.name
        service_name = self.model.app.name

        routes = interfaces['ingress']
        if routes:
            routes = list(routes.get_data().values())
        else:
            routes = []

        if not all(r.get("service") for r in routes):
            self.model.unit.status = WaitingStatus("Waiting for ingress connection information.")
            return

        virtual_services = [
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
                            'rewrite': {'uri': route.get('rewrite', route['prefix'])},
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
            for route in routes
        ]

        gateway = {
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

        auth_routes = interfaces['ingress-auth']
        if auth_routes:
            auth_routes = list(auth_routes.get_data().values())
        else:
            auth_routes = []

        if not all(ar.get("service") for ar in auth_routes):
            self.model.unit.status = WaitingStatus("Waiting for auth route connection information.")
            return

        if auth_routes:
            rbacs = [
                {
                    'apiVersion': 'rbac.istio.io/v1alpha1',
                    'kind': 'RbacConfig',
                    'metadata': {'name': 'default'},
                    'spec': {'mode': 'OFF'},
                }
            ]
        else:
            rbacs = []

        auth_filters = [
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
                                            'patterns': [
                                                {'exact': h}
                                                for h in route.get('allowed-request-headers', [])
                                            ],
                                        }
                                    },
                                    'authorizationResponse': {
                                        'allowedUpstreamHeaders': {
                                            'patterns': [
                                                {'exact': h}
                                                for h in route.get('allowed-response-headers', [])
                                            ],
                                        },
                                    },
                                    'serverUri': {
                                        'cluster': f'outbound|{route["port"]}||{route["service"]}.{namespace}.svc.cluster.local',
                                        'failureModeAllow': False,
                                        'timeout': '10s',
                                        'uri': f'http://{route["service"]}.{namespace}.svc.cluster.local:{route["port"]}',
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
            for route in auth_routes
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
                            "--domain=cluster.local",
                            f"--secureGrpcAddr={config.get('secure-grpc-address')}",
                            "--trust-domain=cluster.local",
                            "--keepaliveMaxServerConnectionAge=30m",
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
                    'customResources': {
                        'gateways.networking.istio.io': [gateway],
                        'virtualservices.networking.istio.io': virtual_services,
                        'rbacconfigs.rbac.istio.io': rbacs,
                        'envoyfilters.networking.istio.io': auth_filters,
                    },
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
                                                + [route['service'] for route in routes],
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

    def _check_leader(self):
        if not self.unit.is_leader():
            raise CheckFailed("Waiting for leadership", WaitingStatus)

    def _get_interfaces(self):
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise CheckFailed(str(err), WaitingStatus)
        except NoCompatibleVersions as err:
            raise CheckFailed(str(err), BlockedStatus)
        return interfaces

    def _check_image_details(self):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            raise CheckFailed(f"{e.status_message}: oci-image", e.status_type)
        return image_details


class CheckFailed(Exception):
    """ Raise this exception if one of the checks in main fails. """

    def __init__(self, msg, status_type=None):
        super().__init__()

        self.msg = msg
        self.status_type = status_type
        self.status = status_type(msg)


if __name__ == "__main__":
    main(Operator)
