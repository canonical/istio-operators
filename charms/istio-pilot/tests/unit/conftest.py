from unittest.mock import MagicMock

import pytest
from lightkube.core.exceptions import ApiError
from ops.testing import Harness

from charm import Operator


class Helpers:
    @staticmethod
    def get_deleted_resource_types(delete_calls):
        deleted_resource_types = []
        for call in delete_calls:
            print(f"resource type: {call[0][1]}")
            resource_type = call[0][1]
            deleted_resource_types.append(resource_type)
        return deleted_resource_types

    @staticmethod
    def compare_deleted_resource_names(actual, expected):
        if len(actual) != len(expected):
            return False
        else:
            return all(elem in expected for elem in actual)

    @staticmethod
    def calls_contain_namespace(calls, namespace):
        for call in calls:
            # Ensure the namespace is included in the call
            if call.kwargs["namespace"] != namespace:
                return False
        return True

    @staticmethod
    def calls_to_tuple_kind_name(calls):
        result = []
        for call in calls:
            resource = call.args[0]
            result.append((resource.kind, resource.metadata.name))
        return result


class DeleteError(ApiError):
    def __init__(self):
        self.status = MagicMock()
        self.status.message = "placeholder"


@pytest.fixture(scope="session")
def helpers():
    return Helpers()


@pytest.fixture(scope="session")
def api_error_mock():
    api_exception = DeleteError()
    yield api_exception


# autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("resources_handler.Client")
    yield client


# TODO: once we extract the _get_gateway_address method
# from the charm code, this bit won't be necessary
@pytest.fixture(autouse=True)
def mocked_charm_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


# Similar to what is done for list, but for get
# and just returning the status attribute
@pytest.fixture(autouse=True)
def mocked_get(mocked_charm_client, mocker):
    mocked_resource_obj = mocker.MagicMock()
    mocked_metadata = mocker.MagicMock()
    mocked_metadata.status = "status"
    mocked_resource_obj.metadata = mocked_metadata

    mocked_charm_client.return_value.get.side_effect = mocked_resource_obj


# autouse to ensure we don't accidentally call out, but
# can also be used explicitly to get access to the mock.
@pytest.fixture(autouse=True)
def mocked_check_call(mocker):
    mocked_check_call = mocker.patch("charm.subprocess.check_call")
    mocked_check_call.return_value = 0

    yield mocked_check_call


# autouse to ensure we don't accidentally call out, but
# can also be used explicitly to get access to the mock.
@pytest.fixture(autouse=True)
def mocked_check_output(mocker):
    mocked_check_output = mocker.patch("charm.subprocess.check_output")
    mocked_check_output.return_value = "stdout"

    yield mocked_check_output



@pytest.fixture
def harness():
    return Harness(Operator)
