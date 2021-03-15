import pytest

from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness
import yaml

from charm import Operator


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin()
    assert isinstance(harness.charm.model.unit.status, WaitingStatus)


def test_missing_image(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert isinstance(harness.charm.model.unit.status, BlockedStatus)


def test_main_no_relation(harness):
    harness.set_leader(True)
    harness.add_oci_resource(
        "oci-image",
        {
            "registrypath": "docker.io/istio/pilot:1.5.0",
            "username": "",
            "password": "",
        },
    )
    harness.begin_with_initial_hooks()
    pod_spec = harness.get_pod_spec()

    # confirm that we can serialize the pod spec
    yaml.safe_dump(pod_spec)

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_main_service_mesh(harness):
    harness.set_leader(True)
    harness.add_oci_resource(
        "oci-image",
        {
            "registrypath": "docker.io/istio/pilot:1.5.0",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("service-mesh", "app")
    harness.begin_with_initial_hooks()
    harness.add_relation_unit(rel_id, "app/0")
    data = {
        "prefix": "/app",
        "rewrite": "/app",
        "service": "my-service",
        "port": 4242,
        "ingress": False,
        "auth": {},
    }
    harness.update_relation_data(
        rel_id,
        "app",
        {"data": yaml.dump(data)},
    )
    assert isinstance(harness.charm.model.unit.status, ActiveStatus)
    pod_spec = harness.get_pod_spec()
    for virtual_service in pod_spec[1]["kubernetesResources"]["customResources"][
        "virtualservices.networking.istio.io"
    ]:
        if "/app" == virtual_service["spec"]["http"][0]["match"][0]["uri"]["prefix"]:
            assert True
            break
    else:
        assert False
