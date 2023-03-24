from contextlib import nullcontext as does_not_raise
from unittest.mock import MagicMock
from unittest.mock import call as Call  # noqa: N812

import pytest
import yaml
from charmed_kubeflow_chisme.exceptions import GenericCharmRuntimeError
from lightkube import codecs
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_global_resource
from ops.model import ActiveStatus, WaitingStatus

from charm import _get_gateway_address_from_svc, _validate_upgrade_version
from istioctl import IstioctlError


@pytest.fixture(autouse=True)
def mocked_list(mocked_client, mocker):
    mocked_resource_obj = mocker.MagicMock()

    def side_effect(*args, **kwargs):
        # List needs to return a list of at least one object of the passed in resource type
        # so that delete gets called
        # Additionally, lightkube's delete method takes in the class name of the object,
        # and the name of the object being deleted as arguments.
        # Unfortunately, making type(some_mocked_object) return a type other than
        # 'unittest.mock.MagicMock does not seem possible. So when checking that the correct
        # resources are being deleted we will check the name of the object being deleted and just
        # use the the class name for obj.metadata.name
        mocked_metadata = mocker.MagicMock()
        mocked_metadata.name = str(args[0].__name__)
        mocked_resource_obj.metadata = mocked_metadata
        return [mocked_resource_obj]

    mocked_client.return_value.list.side_effect = side_effect


def test_events(harness, mocker):
    mocker.patch("lightkube.codecs.load_all_yaml")
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    harness.set_leader(True)
    harness.begin_with_initial_hooks()

    install = mocker.patch("charm.Operator.install")
    remove = mocker.patch("charm.Operator.remove")
    send_info = mocker.patch("charm.Operator.send_info")
    handle_ingress = mocker.patch("charm.Operator.handle_ingress")
    handle_ingress_auth = mocker.patch("charm.Operator.handle_ingress_auth")
    upgrade_charm = mocker.patch("charm.Operator.upgrade_charm")

    harness.charm.on.install.emit()
    install.assert_called_once()
    install.reset_mock()

    harness.charm.on.remove.emit()
    remove.assert_called_once()
    remove.reset_mock()

    harness.charm.on.upgrade_charm.emit()
    upgrade_charm.assert_called_once()
    upgrade_charm.reset_mock()

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
    handle_ingress.assert_called()
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
    assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


def test_basic(harness, mocked_check_call, mocker):
    mocker.patch("lightkube.codecs.load_all_yaml")
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    # Mock _is_gateway_service_up() to simulate that we do see a gateway from istio-ingressgateway
    mocker.patch("charm.Operator._is_gateway_service_up", return_value=True)

    harness.set_leader(True)
    harness.begin_with_initial_hooks()

    expected_args = [
        "./istioctl",
        "install",
        "-y",
        "-s",
        "profile=minimal",
        "-s",
        "values.global.istioNamespace=None",
    ]

    assert len(mocked_check_call.call_args_list) == 1
    assert mocked_check_call.call_args_list[0].args == (expected_args,)
    assert mocked_check_call.call_args_list[0].kwargs == {}

    assert harness.charm.model.unit.status == ActiveStatus("")


def test_with_ingress_relation(
    harness, mocked_check_call, mocked_client, helpers, mocker, mocked_list
):
    # Mock _is_gateway_service_up() to simulate that we do see a gateway from istio-ingressgateway
    mocker.patch("charm.Operator._is_gateway_service_up", return_value=True)

    harness.set_leader(True)

    rel_id = harness.add_relation("ingress", "app")
    harness.add_relation_unit(rel_id, "app/0")
    data = {"service": "service-name", "port": 6666, "prefix": "/"}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    # No need to begin with all initial hooks. This will prevent
    # us from mocking all event handlers that run initially.
    mocker.patch("resources_handler.load_in_cluster_generic_resources")
    harness.begin()
    harness.charm.on.install.emit()

    assert mocked_check_call.call_args_list == [
        Call(
            [
                "./istioctl",
                "install",
                "-y",
                "-s",
                "profile=minimal",
                "-s",
                "values.global.istioNamespace=None",
            ]
        )
    ]
    # Reset the mock so any calls due to previous event triggers are not counted,
    # and then update the ingress relation, triggering the relation_changed event
    mocked_client.reset_mock()

    # Create VirtualService resource
    create_global_resource(
        group="networking.istio.io",
        version="v1alpha3",
        kind="VirtualService",
        plural="virtualservices",
        verbs=None,
    )

    apply_expected = [
        {
            "apiVersion": "networking.istio.io/v1alpha3",
            "kind": "VirtualService",
            "metadata": {
                "name": "service-name",
                "labels": {"app.istio-pilot.io/is-workload-entity": "true"},
            },
            "spec": {
                "gateways": ["None/istio-gateway"],
                "hosts": ["*"],
                "http": [
                    {
                        "match": [{"uri": {"prefix": "/"}}],
                        "rewrite": {"uri": "/"},
                        "route": [
                            {
                                "destination": {
                                    "host": "service-name.None.svc.cluster.local",
                                    "port": {"number": 6666},
                                }
                            }
                        ],
                    }
                ],
            },
        },
    ]

    # Mocks `in_left_not_right`
    mocked_ilnr = mocker.patch("resources_handler.in_left_not_right")
    mocked_ilnr.return_value = [codecs.from_dict(apply_expected[0])]

    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )

    delete_calls = mocked_client.return_value.delete.call_args_list
    assert helpers.calls_contain_namespace(delete_calls, harness.model.name)
    actual_res_names = helpers.get_deleted_resource_types(delete_calls)

    expected_res_names = ["service-name"]
    assert helpers.compare_deleted_resource_names(actual_res_names, expected_res_names)

    apply_calls = mocked_client.return_value.apply.call_args_list
    assert helpers.calls_contain_namespace(apply_calls, harness.model.name)
    apply_args = []
    for call in apply_calls:
        apply_args.append(call[0][0])
    assert apply_args == apply_expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_with_ingress_auth_relation(harness, mocked_check_call, helpers, mocked_client, mocker):
    harness.set_leader(True)
    rel_id = harness.add_relation("ingress-auth", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {
        "service": "service-name",
        "port": 6666,
        "allowed-request-headers": ["foo"],
        "allowed-response-headers": ["bar"],
    }
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    # No need to begin with all initial hooks. This will prevent
    # us from mocking all event handlers that run initially.
    mocker.patch("resources_handler.load_in_cluster_generic_resources")
    harness.begin()
    harness.charm.on.install.emit()
    assert mocked_check_call.call_args_list == [
        Call(
            [
                "./istioctl",
                "install",
                "-y",
                "-s",
                "profile=minimal",
                "-s",
                "values.global.istioNamespace=None",
            ]
        )
    ]

    # Reset the mock so any calls due to previous event triggers are not counted,
    # and then update the ingress relation, triggering the relation_changed event
    mocked_client.reset_mock()
    create_global_resource(
        group="networking.istio.io",
        version="v1alpha3",
        kind="EnvoyFilter",
        plural="envoyfilters",
        verbs=None,
    )

    expected = [
        {
            "apiVersion": "networking.istio.io/v1alpha3",
            "kind": "EnvoyFilter",
            "metadata": {
                "name": "authn-filter",
                "labels": {"app.istio-pilot.io/is-workload-entity": "true"},
            },
            "spec": {
                "configPatches": [
                    {
                        "applyTo": "HTTP_FILTER",
                        "match": {
                            "context": "GATEWAY",
                            "listener": {
                                "portNumber": 8080,
                                "filterChain": {
                                    "filter": {
                                        "name": "envoy.filters.network.http_connection_manager"
                                    }
                                },
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
                                        "server_uri": {
                                            "uri": "http://service-name.None.svc.cluster.local:6666",  # noqa: E501
                                            "cluster": "outbound|6666||service-name.None.svc."
                                            "cluster.local",
                                            "timeout": "10s",
                                        },
                                        "authorization_request": {
                                            "allowed_headers": {"patterns": [{"exact": "foo"}]}
                                        },
                                        "authorization_response": {
                                            "allowed_upstream_headers": {
                                                "patterns": [{"exact": "bar"}]
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
        }
    ]

    # Mocks `in_left_not_right`
    mocked_ilnr = mocker.patch("resources_handler.in_left_not_right")
    mocked_ilnr.return_value = [codecs.from_dict(expected[0])]
    harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )
    delete_calls = mocked_client.return_value.delete.call_args_list
    assert helpers.calls_contain_namespace(delete_calls, harness.model.name)
    actual_res_names = helpers.get_deleted_resource_types(delete_calls)
    expected_res_names = ["EnvoyFilter"]
    assert helpers.compare_deleted_resource_names(actual_res_names, expected_res_names)

    apply_calls = mocked_client.return_value.apply.call_args_list
    assert helpers.calls_contain_namespace(apply_calls, harness.model.name)
    apply_args = []
    for call in apply_calls:
        apply_args.append(call[0][0])
    assert apply_args == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_correct_data_in_gateway_info_relation(harness, mocker, mocked_client):
    harness.set_leader(True)

    create_global_resource(
        group="networking.istio.io",
        version="v1beta1",
        kind="Gateway",
        plural="gateways",
        verbs=None,
    )

    mocked_validate_gateway = mocker.patch(
        "resources_handler.ResourceHandler.validate_resource_exist"
    )
    mocked_validate_gateway.return_value = True

    harness.set_model_name("test-model")
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    rel_id = harness.add_relation("gateway-info", "app")
    harness.add_relation_unit(rel_id, "app/0")

    harness.begin_with_initial_hooks()
    model = harness.charm.framework.model
    gateway_relations = model.get_relation("gateway-info", rel_id)
    istio_relation_data = gateway_relations.data[harness.charm.app]

    assert istio_relation_data["gateway_name"] == harness.model.config["default-gateway"]
    assert istio_relation_data["gateway_namespace"] == model.name


def test_removal(harness, mocked_check_output, mocked_client, helpers, mocker):
    # Mock this method to avoid an error when passing mocked manifests
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    # Mock delete_manifest to avoid loading a mocked manifest when calling load_all_yaml
    # inside delete manifest.
    # FIXME: by mocking this we are not testing that the manifests created by istio and
    # retrieved by `istioctl manifest generate...` are deleted correctly. We should find
    # a way to test this without affecting the tests of other k8s resources created and
    # removed by the charm. In the past, we mocked `load_all_yaml`, and that worked because
    # this method could be found nowhere else in the resources_handler code, due to recent
    # changes in Lightkube's API, we use `load_all_yaml` other places, which makes conflicts
    # with other parts of this test if mocked.
    mocker.patch("resources_handler.ResourceHandler.delete_manifest")

    harness.set_leader(True)

    # No need to begin with all initial hooks. This will prevent
    # us from mocking all event handlers that run initially.
    harness.begin()
    harness.charm.on.install.emit()

    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()

    expected_args = [
        "./istioctl",
        "manifest",
        "generate",
        "-s",
        "profile=minimal",
        "-s",
        f"values.global.istioNamespace={None}",
    ]

    harness.charm.on.remove.emit()
    assert len(mocked_check_output.call_args_list) == 1
    assert mocked_check_output.call_args_list[0].args == (expected_args,)
    assert mocked_check_output.call_args_list[0].kwargs == {}

    delete_calls = mocked_client.return_value.delete.call_args_list
    assert helpers.calls_contain_namespace(delete_calls, harness.model.name)
    actual_res_names = helpers.get_deleted_resource_types(delete_calls)

    expected_res_names = [
        "Gateway",
        "EnvoyFilter",
        "VirtualService",
    ]
    assert helpers.compare_deleted_resource_names(actual_res_names, expected_res_names)


def test_remove_exceptions(harness, mocked_client, mocker):
    mocked_metadata = mocker.MagicMock()
    mocked_metadata.name = "ResourceObjectFromYaml"
    mocked_yaml_object = mocker.MagicMock(metadata=mocked_metadata)
    mocker.patch(
        "resources_handler.codecs.load_all_yaml",
        return_value=[mocked_yaml_object, mocked_yaml_object],
    )

    # Mock this method to avoid an error when passing mocked manifests
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    harness.set_leader(True)
    harness.begin()

    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()
    # Now test the exceptions that should be ignored
    # ApiError
    api_error = ApiError(response=mocker.MagicMock())
    # # ApiError with not found message should be ignored
    api_error.status.message = "something not found"
    mocked_client.return_value.delete.side_effect = api_error
    # mock out the _delete_existing_resources method since we dont want the ApiError
    # to be thrown there
    mocker.patch("resources_handler.ResourceHandler.delete_existing_resources")
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


@pytest.fixture()
def mock_nodeport_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{}]}},
            "spec": {"type": "NodePort", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_clusterip_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{}]}},
            "spec": {"type": "ClusterIP", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_ip_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{"ip": "127.0.0.1"}]}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_hostname_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{"hostname": "test.com"}]}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_ip_service_not_ready():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": []}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_hostname_service_not_ready():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": []}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.mark.parametrize(
    "mock_service_fixture, is_gateway_up",
    [
        # Pass fixtures by their names
        ("mock_nodeport_service", True),
        ("mock_clusterip_service", True),
        ("mock_loadbalancer_hostname_service", True),
        ("mock_loadbalancer_ip_service", True),
        ("mock_loadbalancer_hostname_service_not_ready", False),
        ("mock_loadbalancer_ip_service_not_ready", False),
    ],
)
def test_is_gateway_service_up(mock_service_fixture, is_gateway_up, harness, request):
    harness.begin()

    mock_get_gateway_service = MagicMock(
        return_value=request.getfixturevalue(mock_service_fixture)
    )

    harness.charm._get_gateway_service = mock_get_gateway_service
    assert harness.charm._is_gateway_service_up() is is_gateway_up


@pytest.mark.parametrize(
    "mock_service_fixture, gateway_address",
    [
        # Pass fixtures by their names
        ("mock_nodeport_service", None),
        ("mock_clusterip_service", "10.10.10.10"),
        ("mock_loadbalancer_hostname_service", "test.com"),
        ("mock_loadbalancer_ip_service", "127.0.0.1"),
        ("mock_loadbalancer_hostname_service_not_ready", None),
        ("mock_loadbalancer_ip_service_not_ready", None),
    ],
)
def test_get_gateway_address_from_svc(
    mock_service_fixture,
    gateway_address,
    harness,
    mocked_check_output,
    mocked_client,
    helpers,
    mocker,
    mocked_list,
    request,
):
    """Test that the charm._gateway_address correctly returns gateway service IP/hostname."""
    mock_service = request.getfixturevalue(mock_service_fixture)

    assert _get_gateway_address_from_svc(svc=mock_service) is gateway_address


@pytest.fixture()
def mocked_istioctl_class(mocker):
    mocked_istioctl_class = mocker.patch("charm.Istioctl")
    mocked_istioctl_class.return_value = mocked_istioctl
    yield mocked_istioctl_class


@pytest.fixture()
def mocked_istioctl(mocked_istioctl_class):
    mocked_istioctl = MagicMock()
    mocked_istioctl_class.return_value = mocked_istioctl
    yield mocked_istioctl


def test_upgrade_successful(harness, mocked_istioctl, mocked_istioctl_class, mocker):
    """Tests that charm.upgrade_charm works successfully when expected."""
    model_name = "test-model"
    harness.set_model_name(model_name)
    harness.set_leader(True)
    # Avoid initialising the resource handler
    mocker.patch("resources_handler.load_in_cluster_generic_resources")
    # Do not validate versions
    mocker.patch("charm._validate_upgrade_version")

    harness.begin()

    harness.charm.upgrade_charm("mock_event")

    mocked_istioctl_class.assert_called_with("./istioctl", model_name, "minimal")
    mocked_istioctl.upgrade.assert_called_with()


@pytest.fixture()
def mocked_istioctl_precheck(mocker):
    mocker.patch("charm.Istioctl.precheck", side_effect=IstioctlError())


def test_upgrade_failed_precheck(harness, mocked_istioctl_precheck, mocker):
    """Tests that charm.upgrade_charm fails when precheck fails."""
    model_name = "test-model"
    harness.set_model_name(model_name)
    harness.set_leader(True)
    # Avoid initialising the resource handler
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    harness.begin()

    with pytest.raises(GenericCharmRuntimeError):
        harness.charm.upgrade_charm("mock_event")


@pytest.fixture()
def mocked_istioctl_version(mocker):
    mocker.patch("charm.Istioctl.precheck", side_effect=IstioctlError())


def test_upgrade_failed_getting_version(harness, mocked_istioctl_version, mocker):
    """Tests that charm.upgrade_charm fails when precheck fails."""
    model_name = "test-model"
    harness.set_model_name(model_name)
    harness.set_leader(True)
    # Avoid initialising the resource handler
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    harness.begin()

    with pytest.raises(GenericCharmRuntimeError):
        harness.charm.upgrade_charm("mock_event")


def test_upgrade_failed_version_check(harness, mocker):
    """Tests that charm.upgrade_charm fails when precheck fails."""
    model_name = "test-model"
    harness.set_model_name(model_name)
    harness.set_leader(True)
    # Avoid initialising the resource handler
    mocker.patch("resources_handler.load_in_cluster_generic_resources")
    # Mock _validate_upgrade_version to raise a failure
    mocker.patch("charm._validate_upgrade_version", side_effect=ValueError())

    harness.begin()

    with pytest.raises(GenericCharmRuntimeError):
        harness.charm.upgrade_charm("mock_event")


@pytest.fixture()
def mocked_istioctl_upgrade(mocker):
    mocker.patch("charm.Istioctl.upgrade", side_effect=IstioctlError())


def test_upgrade_failed_during_upgrade(harness, mocked_istioctl_upgrade, mocker):
    """Tests that charm.upgrade_charm fails when upgrade process fails."""
    model_name = "test-model"
    harness.set_model_name(model_name)
    harness.set_leader(True)
    # Avoid initialising the resource handler
    mocker.patch("resources_handler.load_in_cluster_generic_resources")

    harness.begin()

    with pytest.raises(GenericCharmRuntimeError):
        harness.charm.upgrade_charm("mock_event")


@pytest.mark.parametrize(
    "versions, context_raised",
    [
        ({"client": "1.1.0", "control_plane": "1.1.0"}, does_not_raise()),
        ({"client": "1.1.1", "control_plane": "1.1.0"}, does_not_raise()),
        ({"client": "1.2.10", "control_plane": "1.1.0"}, does_not_raise()),
        ({"client": "2.1.0", "control_plane": "1.1.0"}, pytest.raises(ValueError)),
        ({"client": "1.1.0", "control_plane": "1.2.0"}, pytest.raises(ValueError)),
        ({"client": "1.1.0", "control_plane": "2.1.0"}, pytest.raises(ValueError)),
    ],
)
def test_validate_upgrade_version(versions, context_raised):
    with context_raised:
        _validate_upgrade_version(versions)
