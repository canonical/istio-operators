from unittest.mock import patch

import pytest
import yaml
from lightkube.core.exceptions import ApiError
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus


def test_events(configured_harness, mocker):
    start = mocker.patch("charm.Operator.start")
    remove = mocker.patch("charm.Operator.remove")

    configured_harness.charm.on.start.emit()
    start.assert_called_once()
    start.reset_mock()

    configured_harness.charm.on.remove.emit()
    remove.assert_called_once()
    remove.reset_mock()

    configured_harness.charm.on.config_changed.emit()
    start.assert_called_once()
    start.reset_mock()

    rel_id = configured_harness.add_relation("istio-pilot", "app")
    configured_harness.update_relation_data(
        rel_id,
        "app",
        {"some_key": "some_value"},
    )
    start.assert_called_once()
    start.reset_mock()


def test_install_not_leader(harness):
    harness.begin_with_initial_hooks()
    assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


def test_install_no_kind(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert harness.charm.model.unit.status == BlockedStatus("Config item `kind` must be set")


def test_install_no_rel(harness):
    harness.set_leader(True)
    harness.update_config({"kind": "ingress"})
    harness.begin_with_initial_hooks()

    assert harness.charm.model.unit.status == BlockedStatus(
        "Please add required relation to istio-pilot"
    )


def test_start_apply(configured_harness, kind, mocked_client):
    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()

    configured_harness.charm.on.start.emit()
    actual_objects = []
    expected_objects = list(yaml.safe_load_all(open(f"tests/unit/data/{kind}-example.yaml")))

    # the apply method is called for every object in the manifest
    for call in mocked_client.return_value.apply.call_args_list:
        # Ensure the server side apply calls include the namespace kwarg
        assert call.kwargs["namespace"] == "None"
        # The first (and only) argument to the apply method is the obj
        # Convert the object to a dictionary and add it to the list
        actual_objects.append(call.args[0].to_dict())

    assert expected_objects == actual_objects
    assert configured_harness.charm.model.unit.status == ActiveStatus("")


def test_removal(configured_harness, kind, mocked_client, mocker):
    mocked_client.reset_mock()
    configured_harness.charm.on.remove.emit()

    # Ensure the objects that get deleted are the objects defined in the example yaml files
    actual_kind_name_list = []
    expected_objects = list(yaml.safe_load_all(open(f"tests/unit/data/{kind}-example.yaml")))
    expected_kind_name_list = []
    for obj in expected_objects:
        kind_name = {"kind": obj["kind"], "name": obj["metadata"]["name"]}
        expected_kind_name_list.append(kind_name)

    for call in mocked_client.return_value.delete.call_args_list:
        # Ensure the delete calls include the namespace kwarg ('None' in the example yaml)
        assert call.kwargs["namespace"] == "None"
        # The first argument is the resource class
        # The second argument is the object name
        kind_name = {"kind": call.args[0].__name__, "name": call.args[1]}
        actual_kind_name_list.append(kind_name)

    assert expected_kind_name_list == actual_kind_name_list

    # Test exceptions
    # ApiError with unauthorized message should be ignored
    api_error = ApiError(response=mocker.MagicMock())
    api_error.status.message = "(Unauthorized)"
    mocked_client.return_value.delete.side_effect = api_error
    # Ensure we DO NOT raise the exception
    configured_harness.charm.on.remove.emit()

    # Other ApiErrors should raise exceptions
    api_error = ApiError(response=mocker.MagicMock())
    api_error.status.message = "mocked error"
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        configured_harness.charm.on.remove.emit()

    # Test with nonexistent status message
    api_error.status.message = None
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        configured_harness.charm.on.remove.emit()


def test_service_type(configured_harness_only_ingress, gateway_service_type, mocked_client):
    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()

    configured_harness_only_ingress.charm.on.start.emit()
    actual_objects = []

    # the apply method is called for every object in the manifest
    for call in mocked_client.return_value.apply.call_args_list:
        # Ensure the server side apply calls include the namespace kwarg
        assert call.kwargs["namespace"] == "None"
        # The first (and only) argument to the apply method is the obj
        # Convert the object to a dictionary and add it to the list
        actual_objects.append(call.args[0].to_dict())

    services = filter(lambda obj: obj.get("kind") == "Service", actual_objects)
    ingress_workloads = filter(
        lambda obj: obj["metadata"].get("name") == "istio-ingressgateway-workload", services
    )
    workload_service = list(ingress_workloads)[0]

    assert workload_service["spec"].get("type") == gateway_service_type
    assert configured_harness_only_ingress.charm.model.unit.status == ActiveStatus("")


def test_metrics(harness):
    """Test MetricsEndpointProvider initialization."""
    with patch("charm.MetricsEndpointProvider") as mock_metrics:
        harness.begin()
        mock_metrics.assert_called_once_with(
            charm=harness.charm,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "metrics_path": "/stats/prometheus",
                    "static_configs": [
                        {"targets": [f"istio-gateway-metrics.{harness.model.name}.svc:9090"]}
                    ],
                }
            ],
        )


def test_manifests_applied_with_anti_affinity(configured_harness, kind, mocked_client):
    """
    Asserts that the Deployment manifest called by `lightkube_client.apply`
    contains the correct anti-affinity rule
    """

    # Arrange
    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()

    # Define the expected labelSelector based on the kind
    expected_label_selector = {"key": "app", "operator": "In", "values": [f"istio-{kind}gateway"]}

    # Act
    configured_harness.charm.on.start.emit()

    actual_objects = []

    # Get all the objects called by lightkube client `.apply`
    for call in mocked_client.return_value.apply.call_args_list:
        # The first (and only) argument to the apply method is the obj
        # Convert the object to a dictionary and add it to the list
        actual_objects.append(call.args[0].to_dict())

    # Filter out the objects with Deployment kind
    deployments = filter(lambda obj: obj.get("kind") == "Deployment", actual_objects)
    # The gateway deployment is the only Deployment object in the manifests
    gateway_deployment = list(deployments)[0]

    # Assert the Deployment has the correct antiaffinity rule
    assert (
        gateway_deployment["spec"]
        .get("template")
        .get("spec")
        .get("affinity")
        .get("podAntiAffinity")
        .get("requiredDuringSchedulingIgnoredDuringExecution")[0]
        .get("labelSelector")
        .get("matchExpressions")[0]
        == expected_label_selector
    )


def test_manifests_applied_with_replicas_config(configured_harness, mocked_client):
    """
    Asserts that the Deployment manifest called by `lightkube_client.apply`
    contains the replicas config value.
    """

    # Arrange
    # Reset the mock so that the calls list does not include any calls from other hooks
    mocked_client.reset_mock()

    # Update the replicas config in the harness
    replicas_config_value = 2
    configured_harness.update_config({"replicas": replicas_config_value})

    # Act
    configured_harness.charm.on.install.emit()

    actual_objects = []

    # Get all the objects called by lightkube client `.apply`
    for call in mocked_client.return_value.apply.call_args_list:
        # The first (and only) argument to the apply method is the obj
        # Convert the object to a dictionary and add it to the list
        actual_objects.append(call.args[0].to_dict())

    # Filter out the objects with Deployment kind
    deployments = filter(lambda obj: obj.get("kind") == "Deployment", actual_objects)
    # The gateway deployment is the only Deployment object in the manifests
    gateway_deployment = list(deployments)[0]

    # Assert
    assert gateway_deployment["spec"].get("replicas") == replicas_config_value
    assert configured_harness.charm.model.unit.status == ActiveStatus("")
