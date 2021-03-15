import pytest

from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness
from unittest.mock import patch
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
            "registrypath": "docker.io/istio/proxyv2:1.5.0",
            "username": "",
            "password": "",
        },
    )
    harness.begin_with_initial_hooks()
    pod_spec = harness.get_pod_spec()

    # confirm that we can serialize the pod spec
    yaml.safe_dump(pod_spec)

    assert isinstance(harness.charm.model.unit.status, BlockedStatus)


@patch("charm.Operator.check_ca_root_cert")
def test_main_with_relation(mock_ca_check, harness):
    class FakeConfigMap:
        data = {"root-cert.pem": "cert"}

    mock_ca_check.return_value = FakeConfigMap()
    harness.set_leader(True)
    harness.add_oci_resource(
        "oci-image",
        {
            "registrypath": "docker.io/istio/proxyv2:1.5.0",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("istio-pilot", "app")
    harness.begin_with_initial_hooks()
    assert isinstance(harness.charm.model.unit.status, WaitingStatus)
    harness.add_relation_unit(rel_id, "app/0")
    harness.update_relation_data(
        rel_id,
        "app",
        {"service-name": "istio-pilot", "service-port": "15010"},
    )

    pod_spec = harness.get_pod_spec()
    yaml.safe_dump(pod_spec)
    assert isinstance(harness.charm.model.unit.status, ActiveStatus)
    assert "istio-pilot:15010" in pod_spec[0]["containers"][0]["args"]
