import pytest
import yaml
from charm import Operator
from ops.testing import Harness


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
def configured_harness(harness, kind):
    harness.set_leader(True)

    harness.update_config({'kind': kind})
    rel_id = harness.add_relation("istio-pilot", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service-name": "service-name", "service-port": '6666'}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    harness.begin_with_initial_hooks()

    return harness


@pytest.fixture(params=["LoadBalancer", "ClusterIP", "NodePort"])
def gateway_service_type(request):
    return request.param


@pytest.fixture()
def configured_harness_only_ingress(harness, gateway_service_type):
    harness.set_leader(True)

    harness.update_config({'kind': 'ingress', 'gateway_service_type': gateway_service_type})
    rel_id = harness.add_relation("istio-pilot", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service-name": "service-name", "service-port": '6666'}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    harness.begin_with_initial_hooks()

    return harness
