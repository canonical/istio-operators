import pytest
import yaml
from charm import Operator
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_no_kind(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert harness.charm.model.unit.status == BlockedStatus('Config item `kind` must be set')


def test_kind_no_rel(harness, mocker):
    harness.set_leader(True)

    harness.update_config({'kind': 'ingress'})
    harness.begin_with_initial_hooks()

    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    assert harness.charm.model.unit.status == BlockedStatus('Waiting for istio-pilot relation')


def test_kind_ingress(harness, mocker):
    run = mocker.patch('subprocess.run')
    harness.set_leader(True)

    harness.update_config({'kind': 'ingress'})
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

    harness.begin_with_initial_hooks()

    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    assert len(run.call_args_list) == 4

    expected_args = (['./kubectl', 'apply', '-f-'],)
    expected_input = list(yaml.safe_load_all(open('tests/ingress-example.yaml')))

    for call in run.call_args_list:
        actual_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == expected_args
        assert expected_input == actual_input

    assert harness.charm.model.unit.status == ActiveStatus('')


def test_kind_egress(harness, mocker):
    run = mocker.patch('subprocess.run')
    harness.set_leader(True)

    harness.update_config({'kind': 'egress'})
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

    harness.begin_with_initial_hooks()

    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    assert len(run.call_args_list) == 4

    expected_args = (['./kubectl', 'apply', '-f-'],)
    expected_input = list(yaml.safe_load_all(open('tests/egress-example.yaml')))

    for call in run.call_args_list:
        actual_input = list(yaml.safe_load_all(call.kwargs['input']))
        assert call.args == expected_args
        assert expected_input == actual_input

    assert harness.charm.model.unit.status == ActiveStatus('')
