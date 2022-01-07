from unittest.mock import call as Call

import pytest
import yaml
from charm import Operator
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness


# autouse to ensure we don't accidentally call out, but
# can also be used explicitly to get access to the mock.
@pytest.fixture(autouse=True)
def subprocess(mocker):
    subprocess = mocker.patch("charm.subprocess")
    for method_name in ("run", "call", "check_call", "check_output"):
        method = getattr(subprocess, method_name)
        method.return_value.returncode = 0
        method.return_value.stdout = b""
        method.return_value.stderr = b""
        method.return_value.output = b""
        mocker.patch(f"subprocess.{method_name}", method)
    yield subprocess


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_basic(harness, subprocess):
    check_call = subprocess.check_call
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


def test_default_gateways(harness, subprocess):
    run = subprocess.run
    check_call = subprocess.check_call

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {"registrypath": "", "username": "", "password": ""},
    )
    harness.begin_with_initial_hooks()

    run.reset_mock()
    check_call.reset_mock()

    default_gateways = "myGateway"
    harness.update_config({"default-gateways": default_gateways})
    gateway_names = default_gateways.split(',')

    # Check deletion calls

    expected_delete_call = Call(
        [
            './kubectl',
            '-n',
            None,
            'delete',
            'gateways',
            '-lapp.juju.is/created-by=istio-pilot,app.istio-pilot.io/is-workload-entity=true',
        ],
        input=None,
        capture_output=False,
        check=True,
    )
    assert expected_delete_call == run.call_args_list[0]

    # Check creation calls

    expected_manifest = [
        {
            'apiVersion': 'networking.istio.io/v1beta1',
            'kind': 'Gateway',
            'metadata': {
                'name': this_gateway_name,
                'labels': {'app.istio-pilot.io/is-workload-entity': "true"},
            },
            'spec': {
                'selector': {'istio': 'ingressgateway'},
                'servers': [
                    {'hosts': ['*'], 'port': {'name': 'http', 'number': 80, 'protocol': 'HTTP'}}
                ],
            },
        }
        for this_gateway_name in gateway_names
    ]

    actual_manifest = list(
        yaml.safe_load_all(run.call_args_list[1].kwargs['input'].decode("utf-8"))
    )
    assert actual_manifest == expected_manifest

    expected_create_call_args = (
        [
            './kubectl',
            '-n',
            None,
            "apply",
            "-f-",
        ],
    )

    assert run.call_args_list[1].args == expected_create_call_args


def test_with_ingress_relation(harness, subprocess):
    run = subprocess.run
    check_call = subprocess.check_call

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {"registrypath": "", "username": "", "password": ""},
    )
    rel_id = harness.add_relation("ingress", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service": "service-name", "port": 6666, "prefix": "/"}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    # To mock `kubectl get svc` call's return of no objs
    run.return_value.stdout = b"{'items': []}"
    harness.begin_with_initial_hooks()

    assert run.call_count == 4
    run.reset_mock()

    # To mock `kubectl get svc` call's return with a loadBalancer ingress ip
    run.return_value.stdout = yaml.safe_dump(
        {"items": [{"status": {"loadBalancer": {"ingress": [{"ip": "127.0.0.1"}]}}}]}
    ).encode("utf-8")
    harness.framework.reemit()

    expected = [
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {
                'name': 'service-name',
                'labels': {'app.istio-pilot.io/is-workload-entity': 'true'},
            },
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

    assert len(run.call_args_list) == 6

    for call in run.call_args_list[1::3]:
        args = [
            './kubectl',
            '-n',
            None,
            'delete',
            'virtualservices',
            '-lapp.juju.is/created-by=istio-pilot,app.istio-pilot.io/is-workload-entity=true',
        ]
        assert call.args == (args,)

    for call in run.call_args_list[2::3]:
        expected_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == (['./kubectl', '-n', None, 'apply', '-f-'],)
        assert expected_input == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_with_multiple_ingress_relations(harness, subprocess):
    run = subprocess.run
    check_call = subprocess.check_call

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {"registrypath": "", "username": "", "password": ""},
    )

    related_apps = ["app0", "app1"]
    for app in related_apps:
        rel_id = harness.add_relation("ingress", app)

        harness.add_relation_unit(rel_id, f"{app}/0")
        data = {"service": f"{app}-service-name", "port": 6666, "prefix": f"/{app}"}
        harness.update_relation_data(
            rel_id,
            app,
            {"_supported_versions": "- v1", "data": yaml.dump(data)},
        )

    run.return_value.stdout = b"{'items': []}"
    harness.begin_with_initial_hooks()

    assert run.call_count == 6
    run.reset_mock()
    run.return_value.stdout = yaml.safe_dump(
        {"items": [{"status": {"loadBalancer": {"ingress": [{"ip": "127.0.0.1"}]}}}]}
    ).encode("utf-8")
    harness.framework.reemit()

    expected = [
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {
                'name': f'{app}-service-name',
                'labels': {'app.istio-pilot.io/is-workload-entity': 'true'},
            },
            'spec': {
                'gateways': ['None/istio-gateway'],
                'hosts': ['*'],
                'http': [
                    {
                        'match': [{'uri': {'prefix': f'/{app}'}}],
                        'rewrite': {'uri': f'/{app}'},
                        'route': [
                            {
                                'destination': {
                                    'host': f'{app}-service-name.None.svc.cluster.local',
                                    'port': {'number': 6666},
                                }
                            }
                        ],
                    }
                ],
            },
        }
        for app in related_apps
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

    assert len(run.call_args_list) == 12

    for call in run.call_args_list[1::3]:
        args = [
            './kubectl',
            '-n',
            None,
            'delete',
            'virtualservices',
            '-lapp.juju.is/created-by=istio-pilot,app.istio-pilot.io/is-workload-entity=true',
        ]
        assert call.args == (args,)

    for call in run.call_args_list[2::3]:
        expected_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == (['./kubectl', '-n', None, 'apply', '-f-'],)
        assert expected_input == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_with_ingress_relation_broken(harness, subprocess):
    run = subprocess.run

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {"registrypath": "", "username": "", "password": ""},
    )
    rel_id = harness.add_relation("ingress", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service": "service-name", "port": 6666, "prefix": "/"}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    # To mock `kubectl get svc` call's return with a loadBalancer ingress ip
    run.return_value.stdout = yaml.safe_dump(
        {"items": [{"status": {"loadBalancer": {"ingress": [{"ip": "127.0.0.1"}]}}}]}
    ).encode("utf-8")
    harness.begin_with_initial_hooks()
    assert run.call_count == 8
    run.reset_mock()

    harness.remove_relation(rel_id)

    # There are no routes in ingress, so we should only call run once to delete old items
    assert run.call_count == 2

    expected_delete_call = Call(
        [
            './kubectl',
            '-n',
            None,
            'delete',
            'virtualservices',
            '-lapp.juju.is/created-by=istio-pilot,app.istio-pilot.io/is-workload-entity=true',
        ],
        input=None,
        capture_output=False,
        check=True,
    )
    assert expected_delete_call == run.call_args_list[1]


def test_with_ingress_auth_relation(harness, subprocess):
    run = subprocess.run
    check_call = subprocess.check_call

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {"registrypath": "", "username": "", "password": ""},
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
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'EnvoyFilter',
            'metadata': {
                'name': 'authn-filter',
                'labels': {'app.istio-pilot.io/is-workload-entity': 'true'},
            },
            "spec": {
                "configPatches": [
                    {
                        "applyTo": "HTTP_FILTER",
                        "match": {
                            "context": "GATEWAY",
                            "listener": {
                                "filterChain": {
                                    "filter": {
                                        "name": "envoy.filters.network.http_connection_manager"
                                    }
                                }
                            },
                        },
                        "patch": {
                            "operation": "INSERT_BEFORE",
                            "value": {
                                "name": "envoy.filters.http.ext_authz",
                                "typed_config": {
                                    "@type": "type.googleapis.com/envoy.extensions.filters.http."
                                    "ext_authz.v3.ExtAuthz",
                                    "http_service": {
                                        'server_uri': {
                                            'uri': 'http://service-name.None.svc.cluster.local:6666',  # noqa: E501
                                            'cluster': 'outbound|6666||service-name.None.svc.'
                                            'cluster.local',
                                            'timeout': '10s',
                                        },
                                        "authorization_request": {
                                            "allowed_headers": {'patterns': [{'exact': 'foo'}]}
                                        },
                                        "authorization_response": {
                                            "allowed_upstream_headers": {
                                                'patterns': [{'exact': 'bar'}]
                                            }
                                        },
                                    },
                                },
                            },
                        },
                    }
                ],
                "workloadSelector": {"labels": {"istio": "ingressgateway"}},
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

    assert len(run.call_args_list) == 6

    for call in run.call_args_list[2::2]:
        args = [
            './kubectl',
            '-n',
            None,
            'delete',
            'envoyfilters',
            '-lapp.juju.is/created-by=istio-pilot,app.istio-pilot.io/is-workload-entity=true',
        ]
        assert call.args == (args,)

    for call in run.call_args_list[5::2]:
        expected_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == (['./kubectl', '-n', None, 'apply', '-f-'],)
        assert expected_input == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)
