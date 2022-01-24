import pytest

from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness
import yaml

from charm import Operator


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin_with_initial_hooks()
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
    rel_id = harness.add_relation("ingress", "app")
    harness.add_relation_unit(rel_id, "app/0")
    data = {
        "prefix": "/app",
        "rewrite": "/app",
        "service": "my-service",
        "port": 4242,
    }
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )
    harness.begin_with_initial_hooks()
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


# def test_main_istio_pilot(harness):
#     harness.set_leader(True)
#     harness.add_oci_resource(
#         "oci-image",
#         {
#             "registrypath": "docker.io/istio/pilot:1.5.0",
#             "username": "",
#             "password": "",
#         },
#     )
#     harness.begin_with_initial_hooks()
#     rel_id = harness.add_relation("istio-pilot", "app")
#     # harness.add_relation_unit(rel_id, "app/0")
#
#     relation = harness.model.get_relation("istio-pilot", rel_id)
#     # data = {
#     #     "service-name": "service",
#     #     "service-port": "666",
#     # }
#     relation.data[relation.app]._is_mutable = lambda: True
#     relation.data[relation.app]["_supported_versions"] = "- v1"
#     # relation.data[relation.app]["data"] = yaml.dump(data)
#     # harness.update_relation_data(
#     #     rel_id,
#     #     "app",
#     #     {"_supported_versions": "- v1", "data": yaml.dump(data)},
#     # )
#     harness.charm.on.istio_pilot_relation_joined.emit(relation)
#
#     assert relation.data[harness.charm.app] == None
