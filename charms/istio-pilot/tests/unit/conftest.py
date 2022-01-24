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
    client = mocker.patch("charm.Client")
    yield client


# Mocking list is necessary since _delete_existing_resource_objects uses it to
# find existing resources
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
