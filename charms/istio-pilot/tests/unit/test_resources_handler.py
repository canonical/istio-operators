from contextlib import nullcontext as does_not_raise
from unittest.mock import MagicMock

import pytest

from lightkube.resources.core_v1 import Pod
from lightkube.models.meta_v1 import ObjectMeta
from resources_handler import (
    ResourceHandler,
    in_left_not_right,
    resource_to_tuple,
    resources_to_dict_of_resources,
    select_resources_by_name,
)

APP_NAME = "app-name"
MODEL_NAME = "model-name"


# function scope, fixture is destroyed at the end of the test
@pytest.fixture(scope="function")
def resource_handler_mocked_client(mocker):
    """Yields a resource_handler with a mocked .lightkube_client"""
    mocker.patch("resources_handler.lightkube.Client")
    rh = ResourceHandler(APP_NAME, MODEL_NAME)
    yield rh


def generate_pod_resource_list(pod_names):
    resources = [Pod(kind="Pod", metadata=ObjectMeta(name=str(name))) for name in pod_names]

    return resources


@pytest.mark.parametrize(
    "desired_resource_names,existing_resource_names,expected_resources_deleted_names",
    [
        (  # 0 desired resources, M existing resources.  Delete: M calls.
            tuple(),  # Iterable of names of resources desired after reconciliation
            ("a", "b", "c"),  # Iterable of names of resources already on cluster before
            ("a", "b", "c"),  # Names of resources expected to be passed to delete_resource
        ),
        (  # N desired, 0 existing resources.  Delete: 0 calls.
            ("a", "b", "c"),
            tuple(),
            tuple(),
        ),
        (  # N desired, M existing resources, some overlap.  Delete: non-overlap calls.
            ("a", "b", "c"),
            ("a", "c", "d"),
            ("d",),
        ),
        (  # N desired, N existing resources, all overlap.  Delete: 0 calls.
            ("a", "b", "c"),
            ("a", "b", "c"),
            tuple(),
        ),
    ],
)
def test_reconcile_desired_resources(
    desired_resource_names,
    existing_resource_names,
    expected_resources_deleted_names,
    resource_handler_mocked_client,
    mocker,
):

    rh = resource_handler_mocked_client
    desired_resources = generate_pod_resource_list(desired_resource_names)
    existing_resources = generate_pod_resource_list(existing_resource_names)
    namespace = "some-namespace"

    ########################
    # Mock away dependencies

    # Simplify checking resource apply/delete actions
    rh.delete_resource = MagicMock()
    rh.apply_manifest = MagicMock()

    # Attach our desired resources to a resource handler with a mocked lightkube client
    rh.lightkube_client.list.return_value = existing_resources

    # Mock load_all_yaml to return our given existing_resources.  This means we can ignore the
    # desired_resources arg below entirely
    mocked_load_all_yaml = mocker.patch("resources_handler.lightkube.codecs.load_all_yaml")
    mocked_load_all_yaml.return_value = desired_resources

    ########################
    # Run the test
    rh.reconcile_desired_resources(
        Pod,
        # desired_resources passed as convention, but they have limited effect
        # because codecs.load_all_yaml is mocked above
        desired_resources="this-is-ignored-due-to-mocking",
        namespace=namespace,
    )

    rh.lightkube_client.return_value.apply
    ########################
    # Check results

    # Assert .list() called with expected labels and namespace
    rh.lightkube_client.list.assert_called_with(
        Pod,
        labels={
            "app.juju.is/created-by": f"{APP_NAME}",
            f"app.{APP_NAME}.io/is-workload-entity": "true",
        },
        namespace=namespace,
    )

    # Assert delete called for every element expected to be deleted
    delete_calls = rh.delete_resource.call_args_list
    if delete_calls is None:
        delete_calls = tuple()
    names_deleted = tuple(c.args[0].metadata.name for c in delete_calls)
    assert sorted(names_deleted) == sorted(expected_resources_deleted_names)

    # Assert apply called if it should have been called
    assert rh.apply_manifest.call_count == 1
    resources_applied = mocked_load_all_yaml.return_value
    if resources_applied is None:
        resources_applied = tuple()
    names_applied = tuple(r.metadata.name for r in resources_applied)
    assert sorted(names_applied) == sorted(desired_resource_names)


# Resources for below tests
POD_LIST_1 = [
    Pod(kind="Pod", metadata=ObjectMeta(name=f"pod-{n}", namespace="some-namespace"))
    for n in range(5)
]
POD_TUPLES_1 = [("Pod", f"pod-{n}") for n in range(0, 5)]
POD_LIST_2 = [
    Pod(kind="Pod", metadata=ObjectMeta(name=f"pod-{n}", namespace="some-namespace"))
    for n in range(3, 7)
]
POD_TUPLES_IN_1_NOT_2 = [("Pod", f"pod-{n}") for n in range(0, 3)]


@pytest.mark.parametrize(
    "left,right,expected_result",
    [
        (POD_LIST_1, POD_LIST_1, []),
        (POD_LIST_1, [], POD_TUPLES_1),
        (POD_LIST_1, POD_LIST_2, POD_TUPLES_IN_1_NOT_2),
    ],
)
def test_in_left_not_right(left, right, expected_result):
    result = in_left_not_right(left, right)

    # Use our serialization methods to compare
    result_as_tuples = list(resources_to_dict_of_resources(result).keys())

    assert sorted(result_as_tuples) == sorted(expected_result)


@pytest.mark.parametrize(
    "resources,names_selected,context_raised",
    [
        (POD_LIST_1, ("pod-1", "pod-3"), does_not_raise()),  # Works
        (POD_LIST_1, ("not-a-real-resource",), pytest.raises(KeyError)),  # Missing element
        (POD_LIST_1 + POD_LIST_1, None, pytest.raises(ValueError)),  # Duplicate in resources
    ],
)
def test_select_resources_by_name(resources, names_selected, context_raised):
    with context_raised as err:
        selected = select_resources_by_name(resources, names_selected)

    if err is None:
        expected_result = tuple(("Pod", name) for name in names_selected)
        selected_serialized = tuple(resources_to_dict_of_resources(selected).keys())
        assert selected_serialized == expected_result


@pytest.mark.parametrize(
    "resource,expected,context_raised",
    [
        (  # Successful
            Pod(kind="Pod", metadata=ObjectMeta(name="pod-name", namespace="pod-namespace")),
            ("Pod", "pod-name"),
            does_not_raise(),
        ),
        ({}, None, pytest.raises(AttributeError)),  # Input of incorrect type
    ],
)
def test_resource_to_tuple(resource, expected, context_raised):
    with context_raised as err:
        result = resource_to_tuple(resource)

    if err is None:
        assert sorted(result) == sorted(expected)
