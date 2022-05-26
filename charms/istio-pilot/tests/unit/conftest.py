import pytest
from charm import Operator
from ops.testing import Harness


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
            if call.kwargs['namespace'] != namespace:
                return False
        return True


@pytest.fixture(scope="session")
def helpers():
    return Helpers()


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
    mocked_metadata.status = 'status'
    mocked_resource_obj.metadata = mocked_metadata

    mocked_charm_client.return_value.get.side_effect = mocked_resource_obj


# autouse to ensure we don't accidentally call out, but
# can also be used explicitly to get access to the mock.
@pytest.fixture(autouse=True)
def subprocess(mocker):
    subprocess = mocker.patch("charm.subprocess")
    for method_name in ("check_call", "check_output"):
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
