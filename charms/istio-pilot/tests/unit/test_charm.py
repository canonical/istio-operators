from unittest.mock import call as Call

import pytest
import yaml
from ops.model import ActiveStatus, WaitingStatus
from lightkube.core.exceptions import ApiError


def test_events(harness, mocker):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    install = mocker.patch('charm.Operator.install')
    remove = mocker.patch('charm.Operator.remove')
    send_info = mocker.patch('charm.Operator.send_info')
    handle_ingress = mocker.patch('charm.Operator.handle_ingress')
    handle_ingress_auth = mocker.patch('charm.Operator.handle_ingress_auth')

    harness.charm.on.install.emit()
    install.assert_called_once()
    install.reset_mock()

    harness.charm.on.remove.emit()
    remove.assert_called_once()
    remove.reset_mock()

    rel_id = harness.add_relation("istio-pilot", "app")
    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )
    send_info.assert_called_once()
    send_info.reset_mock()

    rel_id = harness.add_relation("ingress", "app")
    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )
    handle_ingress.assert_called_once()
    handle_ingress.reset_mock()

    harness.add_relation_unit(rel_id, "app/0")
    harness.remove_relation_unit(rel_id, "app/0")
    handle_ingress.assert_called_once()
    handle_ingress.reset_mock()

    rel_id = harness.add_relation("ingress-auth", "app")
    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )
    handle_ingress_auth.assert_called_once()
    handle_ingress_auth.reset_mock()

    harness.add_relation_unit(rel_id, "app/0")
    harness.remove_relation_unit(rel_id, "app/0")
    handle_ingress_auth.assert_called_once()
    handle_ingress_auth.reset_mock()


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_basic(harness, subprocess, mocker):
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


def test_with_ingress_relation(harness, subprocess, mocked_client, helpers, mocker):
    check_call = subprocess.check_call

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

    # Reset the mock so any calls due to previous event triggers are not counted,
    # and then update the ingress relation, triggering the relation_changed event
    mocked_client.reset_mock()
    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )

    apply_expected = [
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

    delete_calls = mocked_client.return_value.delete.call_args_list
    assert helpers.calls_contain_namespace(delete_calls, harness.model.name)
    actual_res_names = helpers.get_deleted_resource_types(delete_calls)

    expected_res_names = ['VirtualService']
    assert helpers.compare_deleted_resource_names(actual_res_names, expected_res_names)

    apply_calls = mocked_client.return_value.apply.call_args_list
    assert helpers.calls_contain_namespace(apply_calls, harness.model.name)
    apply_args = []
    for call in apply_calls:
        apply_args.append(call[0][0])
    assert apply_args == apply_expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_with_ingress_auth_relation(harness, subprocess, helpers, mocked_client, mocker):
    check_call = subprocess.check_call

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

    # Reset the mock so any calls due to previous event triggers are not counted,
    # and then update the ingress relation, triggering the relation_changed event
    mocked_client.reset_mock()
    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )

    expected = [
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'EnvoyFilter',
            'metadata': {
                'name': 'authn-filter',
                'labels': {'app.istio-pilot.io/is-workload-entity': 'true'},
            },
            'spec': {
                'configPatches': [
                    {
                        'applyTo': 'HTTP_FILTER',
                        'match': {
                            'context': 'GATEWAY',
                            'listener': {
                                'filterChain': {
                                    'filter': {
                                        'name': 'envoy.filters.network.http_connection_manager'
                                    }
                                }
                            },
                        },
                        'patch': {
                            'operation': 'INSERT_BEFORE',
                            'value': {
                                'name': 'envoy.filters.http.ext_authz',
                                'typed_config': {
                                    '@type': 'type.googleapis.com/envoy.extensions.filters.http.'
                                    'ext_authz.v3.ExtAuthz',
                                    'http_service': {
                                        'server_uri': {
                                            'uri': 'http://service-name.None.svc.cluster.local:6666',  # noqa: E501
                                            'cluster': 'outbound|6666||service-name.None.svc.'
                                            'cluster.local',
                                            'timeout': '10s',
                                        },
                                        'authorization_request': {
                                            'allowed_headers': {'patterns': [{'exact': 'foo'}]}
                                        },
                                        'authorization_response': {
                                            'allowed_upstream_headers': {
                                                'patterns': [{'exact': 'bar'}]
                                            }
                                        },
                                    },
                                },
                            },
                        },
                    }
                ],
                'workloadSelector': {'labels': {'istio': 'ingressgateway'}},
            },
        }
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

    delete_calls = mocked_client.return_value.delete.call_args_list
    assert helpers.calls_contain_namespace(delete_calls, harness.model.name)
    actual_res_names = helpers.get_deleted_resource_types(delete_calls)
    expected_res_names = ['EnvoyFilter']
    assert helpers.compare_deleted_resource_names(actual_res_names, expected_res_names)

    apply_calls = mocked_client.return_value.apply.call_args_list
    assert helpers.calls_contain_namespace(apply_calls, harness.model.name)
    apply_args = []
    for call in apply_calls:
        apply_args.append(call[0][0])
    assert apply_args == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_removal(harness, subprocess, mocked_client, helpers, mocker):
    check_output = subprocess.check_output

    mocked_metadata = mocker.MagicMock()
    mocked_metadata.name = "ResourceObjectFromYaml"
    mocked_yaml_object = mocker.MagicMock(metadata=mocked_metadata)
    mocker.patch(
        'charm.codecs.load_all_yaml', return_value=[mocked_yaml_object, mocked_yaml_object]
    )

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )

    harness.begin_with_initial_hooks()

    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()
    harness.charm.on.remove.emit()

    expected_args = [
        "./istioctl",
        "manifest",
        "generate",
        "-s",
        "profile=minimal",
        "-s",
        f"values.global.istioNamespace={None}",
    ]

    assert len(check_output.call_args_list) == 1
    assert check_output.call_args_list[0].args == (expected_args,)
    assert check_output.call_args_list[0].kwargs == {}

    delete_calls = mocked_client.return_value.delete.call_args_list
    assert helpers.calls_contain_namespace(delete_calls, harness.model.name)
    actual_res_names = helpers.get_deleted_resource_types(delete_calls)
    # The 2 mock objects at the end are the "resources" that get returned from the mocked
    # load_all_yaml call when loading the resources from the manifest.
    expected_res_names = [
        'VirtualService',
        'Gateway',
        'EnvoyFilter',
        'ResourceObjectFromYaml',
        'ResourceObjectFromYaml',
    ]
    assert helpers.compare_deleted_resource_names(actual_res_names, expected_res_names)

    # Now test the exceptions that should be ignored
    # ApiError
    api_error = ApiError(response=mocker.MagicMock())
    # # ApiError with not found message should be ignored
    api_error.status.message = "something not found"
    mocked_client.return_value.delete.side_effect = api_error
    # mock out the _delete_existing_resource_objects method since we dont want the ApiError
    # to be thrown there
    mocker.patch('charm.Operator._delete_existing_resource_objects')
    # Ensure we DO NOT raise the exception
    harness.charm.on.remove.emit()

    # ApiError with unauthorized message should be ignored
    api_error.status.message = "(Unauthorized)"
    mocked_client.return_value.delete.side_effect = api_error
    # Ensure we DO NOT raise the exception
    harness.charm.on.remove.emit()

    # Other ApiErrors should throw an exception
    api_error.status.message = "mocked ApiError"
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        harness.charm.on.remove.emit()

    # Test with nonexistent status message
    api_error.status.message = None
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        harness.charm.on.remove.emit()
