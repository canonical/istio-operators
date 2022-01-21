from unittest.mock import call as Call

import pytest
import yaml
from charm import Operator
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_basic(harness, mocker):
    check_call = mocker.patch('subprocess.check_call')
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    expected_args = [
        './istioctl',
        'install',
        '-y',
        '-s',
        'profile=minimal',
        '-s',
        'values.global.istioNamespace=None',
    ]

    assert len(check_call.call_args_list) == 1
    assert check_call.call_args_list[0].args == (expected_args,)
    assert check_call.call_args_list[0].kwargs == {}

    assert harness.charm.model.unit.status == ActiveStatus('')


def test_with_ingress_relation(harness, mocker):
    run = mocker.patch('subprocess.run')
    check_call = mocker.patch('subprocess.check_call')

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("ingress", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service": "service-name", "port": 6666, "prefix": "/"}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )
    harness.begin_with_initial_hooks()

    expected = [
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {'name': 'service-name'},
            'spec': {
                'gateways': ['None/istio-gateway'],
                'hosts': ['*'],
                'http': [
                    {
                        'match': [{'uri': {'prefix': '/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'service-name.None.svc.cluster.local',
                                    'port': {'number': 6666},
                                }
                            }
                        ],
                    }
                ],
            },
        },
        {
            'apiVersion': 'networking.istio.io/v1beta1',
            'kind': 'Gateway',
            'metadata': {'name': 'istio-gateway'},
            'spec': {
                'selector': {'istio': 'ingressgateway'},
                'servers': [
                    {'hosts': ['*'], 'port': {'name': 'http', 'number': 80, 'protocol': 'HTTP'}}
                ],
            },
        },
    ]

    assert check_call.call_args_list == [
        Call(
            [
                './istioctl',
                'install',
                '-y',
                '-s',
                'profile=minimal',
                '-s',
                'values.global.istioNamespace=None',
            ]
        )
    ]

    assert len(run.call_args_list) == 4

    for call in run.call_args_list[::2]:
        args = [
            './kubectl',
            'delete',
            'virtualservices,gateways',
            '-lapp.juju.is/created-by=istio-pilot',
            '-n',
            None,
        ]
        assert call.args == (args,)

    for call in run.call_args_list[1::2]:
        expected_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == (['./kubectl', 'apply', '-f-'],)
        assert expected_input == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_with_ingress_auth_relation(harness, mocker):
    run = mocker.patch('subprocess.run')
    check_call = mocker.patch('subprocess.check_call')

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("ingress-auth", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {
        "service": "service-name",
        "port": 6666,
        "allowed-request-headers": ['foo'],
        "allowed-response-headers": ['bar'],
    }
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )
    harness.begin_with_initial_hooks()

    expected = [
        {
            'apiVersion': 'rbac.istio.io/v1alpha1',
            'kind': 'RbacConfig',
            'metadata': {'name': 'default'},
            'spec': {'mode': 'OFF'},
        },
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
                                    'allowedHeaders': {'patterns': [{'exact': 'foo'}]}
                                },
                                'authorizationResponse': {
                                    'allowedUpstreamHeaders': {'patterns': [{'exact': 'bar'}]}
                                },
                                'serverUri': {
                                    'cluster': 'outbound|6666||service-name.None.svc.cluster.local',
                                    'failureModeAllow': False,
                                    'timeout': '10s',
                                    'uri': 'http://service-name.None.svc.cluster.local:6666',
                                },
                            }
                        },
                        'filterName': 'envoy.ext_authz',
                        'filterType': 'HTTP',
                        'insertPosition': {'index': 'FIRST'},
                        'listenerMatch': {'listenerType': 'GATEWAY'},
                    }
                ],
                'workloadLabels': {'istio': 'ingressgateway'},
            },
        },
    ]

    assert check_call.call_args_list == [
        Call(
            [
                './istioctl',
                'install',
                '-y',
                '-s',
                'profile=minimal',
                '-s',
                'values.global.istioNamespace=None',
            ]
        )
    ]

    assert len(run.call_args_list) == 4

    for call in run.call_args_list[::2]:
        args = [
            './kubectl',
            'delete',
            'envoyfilters,rbacconfigs',
            '-lapp.juju.is/created-by=istio-pilot',
            '-n',
            None,
        ]
        assert call.args == (args,)

    for call in run.call_args_list[1::2]:
        expected_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == (['./kubectl', 'apply', '-f-'],)
        assert expected_input == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


@pytest.mark.parametrize(
    "gateways",
    [
        ([]),
        (["default/gateway-A"]),
        (["app/gateway-A", "db/gateway-B"]),
    ],
    ids=[
        'No custom gateway provided',
        'One custom gateway provided',
        'Two custom gateway provided with different namespaces',
    ],
)
def test_with_custom_gateways(harness, mocker, gateways):
    run = mocker.patch('subprocess.run')
    check_call = mocker.patch('subprocess.check_call')

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("ingress", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service": "service-name", "port": 6666, "prefix": "/"}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )
    harness.update_config({"namespaced-custom-gateways": ",".join(gateways)})
    harness.begin_with_initial_hooks()

    expected = [
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {'name': 'service-name'},
            'spec': {
                'gateways': [
                    'None/istio-gateway',
                ]
                + gateways,
                'hosts': ['*'],
                'http': [
                    {
                        'match': [{'uri': {'prefix': '/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'service-name.None.svc.cluster.local',
                                    'port': {'number': 6666},
                                }
                            }
                        ],
                    }
                ],
            },
        },
        {
            'apiVersion': 'networking.istio.io/v1beta1',
            'kind': 'Gateway',
            'metadata': {'name': 'istio-gateway'},
            'spec': {
                'selector': {'istio': 'ingressgateway'},
                'servers': [
                    {'hosts': ['*'], 'port': {'name': 'http', 'number': 80, 'protocol': 'HTTP'}}
                ],
            },
        },
    ]

    assert check_call.call_args_list == [
        Call(
            [
                './istioctl',
                'install',
                '-y',
                '-s',
                'profile=minimal',
                '-s',
                'values.global.istioNamespace=None',
            ]
        )
    ]

    assert len(run.call_args_list) == 4

    for call in run.call_args_list[::2]:
        args = [
            './kubectl',
            'delete',
            'virtualservices,gateways',
            '-lapp.juju.is/created-by=istio-pilot',
            '-n',
            None,
        ]
        assert call.args == (args,)

    for call in run.call_args_list[1::2]:
        expected_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == (['./kubectl', 'apply', '-f-'],)
        assert expected_input == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)
