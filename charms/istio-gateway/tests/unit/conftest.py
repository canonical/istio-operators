import pytest
import yaml
from charm import Operator
from ops.testing import Harness


class Helpers:
    @staticmethod
    def begin_noop(harness):
        # Most of the tests use these lines to kick things off
        harness.begin_with_initial_hooks()
        container = harness.model.unit.get_container('noop')
        harness.charm.on['noop'].pebble_ready.emit(container)


@pytest.fixture(scope="session")
def helpers():
    return Helpers()


@pytest.fixture
def harness():
    return Harness(Operator)


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


# This is used to parameterize tests for both egress and ingress
# If a test uses this fixture, it will be run once for each param in the list
@pytest.fixture(params=["ingress", "egress"])
def kind(request):
    return request.param


@pytest.fixture()
def configured_harness(harness, kind, helpers):
    harness.set_leader(True)

    harness.update_config({'kind': kind})
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("istio-pilot", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service-name": "service-name", "service-port": '6666'}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    helpers.begin_noop(harness)

    return harness
