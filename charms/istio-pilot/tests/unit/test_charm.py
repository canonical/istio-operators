from unittest.mock import Mock

import pytest
from ops.charm import RelationBrokenEvent, RelationChangedEvent, RelationCreatedEvent
from ops.model import WaitingStatus
from ops.testing import Harness

from charm import Operator

# TODO: Fixtures to block lightkube
# TODO: Fixtures to block istioctl


class TestCharmEvents:
    """Test cross-cutting charm behavior.

    TODO: Explain this better
    """

    def test_event_observing(self, harness, mocker):
        harness.begin()
        mocked_install = mocker.patch("charm.Operator.install")
        mocked_remove = mocker.patch("charm.Operator.remove")
        mocked_upgrade_charm = mocker.patch("charm.Operator.upgrade_charm")
        mocked_reconcile = mocker.patch("charm.Operator.reconcile")

        RelationCreatedEvent
        harness.charm.on.install.emit()
        assert_called_once_and_reset(mocked_install)

        harness.charm.on.remove.emit()
        assert_called_once_and_reset(mocked_remove)

        harness.charm.on.upgrade_charm.emit()
        assert_called_once_and_reset(mocked_upgrade_charm)

        exercise_relation(harness, "gateway-info")
        assert mocked_reconcile.call_count == 1
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationCreatedEvent)
        mocked_reconcile.reset_mock()

        exercise_relation(harness, "istio-pilot")
        assert mocked_reconcile.call_count == 3
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationCreatedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationChangedEvent)
        assert isinstance(mocked_reconcile.call_args_list[2][0][0], RelationBrokenEvent)
        mocked_reconcile.reset_mock()

        exercise_relation(harness, "ingress")
        assert mocked_reconcile.call_count == 2
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationChangedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationBrokenEvent)
        mocked_reconcile.reset_mock()

        exercise_relation(harness, "ingress-auth")
        assert mocked_reconcile.call_count == 2
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationChangedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationBrokenEvent)
        mocked_reconcile.reset_mock()

    def test_not_leader(self, harness):
        """Assert that the charm does not perform any actions when not the leader."""
        harness.set_leader(False)
        harness.begin()
        harness.charm.on.config_changed.emit()
        assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


class TestCharmHelpers:
    """Directly test charm helpers and private methods."""

    def test_reconcile_not_leader(self, harness):
        """Assert that the reconcile handler does not perform any actions when not the leader."""
        harness.set_leader(False)
        harness.begin()
        harness.charm.reconcile("mock event")
        assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")


# Fixtures
@pytest.fixture
def harness():
    return Harness(Operator)


# Helpers
def assert_called_once_and_reset(mock: Mock):
    mock.assert_called_once()
    mock.reset_mock()


def exercise_relation(harness, relation_name):
    """Exercises a relation by creating, joining, updating, departing, and breaking it."""
    other_app = "other"
    other_unit = f"{other_app}/0"
    rel_id = harness.add_relation(relation_name, other_app)
    harness.add_relation_unit(rel_id, other_unit)
    harness.update_relation_data(rel_id, other_app, {"some_key": "some_value"})
    harness.remove_relation_unit(rel_id, other_unit)
    harness.remove_relation(rel_id)
