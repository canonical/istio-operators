import logging
from contextlib import nullcontext as does_not_raise
from unittest.mock import MagicMock, Mock, patch

import pytest
import tenacity
from charmed_kubeflow_chisme.exceptions import GenericCharmRuntimeError
from charmed_kubeflow_chisme.lightkube.mocking import FakeApiError
from lightkube.models.admissionregistration_v1 import (
    ServiceReference,
    ValidatingWebhook,
    ValidatingWebhookConfiguration,
    WebhookClientConfig,
)
from lightkube.models.meta_v1 import ObjectMeta
from ops.charm import RelationBrokenEvent, RelationChangedEvent, RelationCreatedEvent
from ops.model import WaitingStatus
from ops.testing import Harness

from charm import Operator, _validate_upgrade_version, _wait_for_update_rollout
from istioctl import IstioctlError

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


class TestCharmUpgrade:
    """Tests for charm upgrade handling."""

    @patch("charm.Operator._patch_istio_validating_webhook")  # Do not patch istio installs
    @patch("charm._wait_for_update_rollout")  # Do not wait for upgrade to finish
    @patch("charm._validate_upgrade_version")  # Do not validate versions
    @patch("charm.Istioctl", return_value=MagicMock())
    def test_upgrade_successful(
        self,
        mocked_istioctl_class,
        _mocked_validate_upgrade_version,
        mocked_wait_for_update_rollout,
        _mocked_patch_istio_validating_webhook,
        harness,
    ):
        """Tests that charm.upgrade_charm works successfully when expected."""
        model_name = "test-model"
        harness.set_model_name(model_name)

        mocked_istioctl = mocked_istioctl_class.return_value

        # Return valid version data from istioctl.versions
        mocked_istioctl.version.return_value = {"client": "1.12.5", "control_plane": "1.12.5"}

        # Simulate the upgrade
        harness.begin()
        harness.charm.upgrade_charm("mock_event")

        # Assert that the upgrade was successful
        mocked_istioctl_class.assert_called_with("./istioctl", model_name, "minimal")
        mocked_istioctl.upgrade.assert_called_with()
        harness.charm._patch_istio_validating_webhook.assert_called_with()

        mocked_wait_for_update_rollout.assert_called_once()

    @patch("charm._validate_upgrade_version")  # Do not validate versions
    @patch("charm.Istioctl.version")  # Pass istioctl version check
    @patch("charm.Istioctl.precheck", side_effect=IstioctlError())  # Fail istioctl precheck
    def test_upgrade_failed_precheck(
        self,
        _mocked_istioctl_precheck,
        _mocked_istioctl_version,
        _mocked_validate_upgrade_version,
        harness,
    ):
        """Tests that charm.upgrade_charm fails when precheck fails."""
        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @patch("charm.Istioctl.version", side_effect=IstioctlError())
    def test_upgrade_failed_getting_version(self, _mocked_istioctl_version, harness):
        """Tests that charm.upgrade_charm fails when precheck fails."""
        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @patch("charm._validate_upgrade_version", side_effect=ValueError())  # Fail when validating
    @patch("charm.Istioctl.version")  # Pass istioctl version check
    def test_upgrade_failed_version_check(
        self, _mocked_istioctl_version, _mocked_validate_upgrade_version, harness
    ):
        """Tests that charm.upgrade_charm fails when precheck fails."""
        model_name = "test-model"
        harness.set_model_name(model_name)

        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @patch("charm.Istioctl.upgrade", side_effect=IstioctlError())  # Fail istioctl upgrade
    def test_upgrade_failed_during_upgrade(self, _mocked_istioctl_upgrade, harness):
        """Tests that charm.upgrade_charm fails when upgrade process fails."""
        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @pytest.mark.parametrize(
        "versions, context_raised",
        [
            ({"client": "1.1.0", "control_plane": "1.1.0"}, does_not_raise()),
            ({"client": "1.1.1", "control_plane": "1.1.0"}, does_not_raise()),
            ({"client": "1.2.10", "control_plane": "1.1.0"}, does_not_raise()),
            ({"client": "2.1.0", "control_plane": "1.1.0"}, pytest.raises(ValueError)),
            ({"client": "1.1.0", "control_plane": "1.2.0"}, pytest.raises(ValueError)),
            ({"client": "1.1.0", "control_plane": "2.1.0"}, pytest.raises(ValueError)),
        ],
    )
    def test_validate_upgrade_version(self, versions, context_raised):
        with context_raised:
            _validate_upgrade_version(versions)

    def test_patch_istio_validating_webhook(self, harness, mocked_lightkube_client):
        """Tests that _patch_istio_validating_webhook works as expected."""
        model_name = "test-model"
        harness.set_model_name(model_name)
        harness.begin()

        mock_vwc = ValidatingWebhookConfiguration(
            metadata=ObjectMeta(name="istiod-default-validator"),
            webhooks=[
                ValidatingWebhook(
                    admissionReviewVersions=[""],
                    name="",
                    sideEffects=None,
                    clientConfig=WebhookClientConfig(
                        service=ServiceReference(
                            name="istiod",
                            namespace="istio-system",
                        )
                    ),
                )
            ],
        )

        mocked_lightkube_client.get.return_value = mock_vwc

        harness.charm._patch_istio_validating_webhook()

        # Confirm we've tried to apply a patched version of the webhook
        patch_call = mocked_lightkube_client.patch.call_args_list[0]
        # Assert the expected webhook is being patched
        assert patch_call[0][1] == "istiod-default-validator"
        # Assert that we've patched to the expected model name
        vwc = patch_call[0][2]
        assert vwc.webhooks[0].clientConfig.service.namespace == model_name

    def test_patch_istio_validating_webhook_for_webhook_does_not_exist(
        self, harness, mocked_lightkube_client
    ):
        """Tests that charm._patch_istio_validating_webhook does not fail if webhook missing."""
        harness.begin()

        mocked_lightkube_client = MagicMock()
        mocked_lightkube_client.get.side_effect = FakeApiError(404)

        harness.charm._patch_istio_validating_webhook()

        # Confirm we've not tried to apply anything
        assert mocked_lightkube_client.patch.call_count == 0

    @patch("charm.Istioctl", return_value=MagicMock())
    def test_wait_for_update_rollout(self, mocked_istioctl_class):
        """Tests that waiting for Istio updates to roll out works as expected."""
        mocked_istioctl = mocked_istioctl_class.return_value

        # Mock istioctl.version so it initially returns client != control_plane, then
        # eventually returns client == control_plane.
        versions_equal = {"client": "1.12.5", "control_plane": "1.12.5"}
        versions_not_equal = {"client": "1.12.5", "control_plane": "1.12.4"}
        mocked_istioctl.version.side_effect = [
            versions_not_equal,
            versions_not_equal,
            versions_not_equal,
            versions_equal,
        ]

        retry_strategy = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0.001),
            reraise=True,
        )

        _wait_for_update_rollout(mocked_istioctl, retry_strategy, logging.getLogger())

        assert mocked_istioctl.version.call_count == 4

    @patch("charm.Istioctl", return_value=MagicMock())
    def test_wait_for_update_rollout_timeout(self, mocked_istioctl):
        """Tests that waiting for Istio updates to roll out raises on timeout."""
        # Mock istioctl.version so it always returns client != control_plane
        versions_not_equal = {"client": "1.12.5", "control_plane": "1.12.4"}
        mocked_istioctl.version.return_value = versions_not_equal

        retry_strategy = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0.001),
            reraise=True,
        )

        with pytest.raises(GenericCharmRuntimeError):
            _wait_for_update_rollout(mocked_istioctl, retry_strategy, logging.getLogger())

        assert mocked_istioctl.version.call_count == 5


# Fixtures
@pytest.fixture
def harness():
    return Harness(Operator)


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


@pytest.fixture()
def mocked_lightkube_client(mocked_lightkube_client_class):
    mocked_instance = MagicMock()
    mocked_lightkube_client_class.return_value = mocked_instance
    yield mocked_instance


@pytest.fixture()
def mocked_lightkube_client_class(mocker):
    mocked = mocker.patch("charm.Client")
    yield mocked


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
