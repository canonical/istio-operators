import logging
from contextlib import nullcontext as does_not_raise
from typing import Optional
from unittest.mock import MagicMock, Mock, patch
import yaml

import pytest
import tenacity
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.lightkube.mocking import FakeApiError
from lightkube import codecs
from lightkube.core.exceptions import ApiError
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

from charm import (
    GATEWAY_HTTP_PORT,
    GATEWAY_HTTPS_PORT,
    Operator,
    _get_gateway_address_from_svc,
    _validate_upgrade_version,
    _wait_for_update_rollout,
    _xor, _remove_envoyfilter,
)
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

    @pytest.mark.parametrize(
        "ssl_crt, ssl_key, expected_port, expected_context",
        [
            ("", "", GATEWAY_HTTP_PORT, does_not_raise()),
            ("x", "x", GATEWAY_HTTPS_PORT, does_not_raise()),
            ("x", "", None, pytest.raises(ErrorWithStatus)),
            ("", "x", None, pytest.raises(ErrorWithStatus)),
        ]
    )
    def test_gateway_port(self, ssl_crt, ssl_key, expected_port, expected_context, harness):
        harness.begin()
        harness.update_config({"ssl-crt": ssl_crt, "ssl-key": ssl_key})

        with expected_context:
            gateway_port = harness.charm._gateway_port
            assert gateway_port == expected_port

    @pytest.mark.parametrize(
        "mock_service_fixture, is_gateway_up",
        [
            # Pass fixtures by their names
            ("mock_nodeport_service", True),
            ("mock_clusterip_service", True),
            ("mock_loadbalancer_hostname_service", True),
            ("mock_loadbalancer_ip_service", True),
            ("mock_loadbalancer_hostname_service_not_ready", False),
            ("mock_loadbalancer_ip_service_not_ready", False),
        ],
    )
    def test_is_gateway_service_up(self, mock_service_fixture, is_gateway_up, harness, request):
        harness.begin()

        mock_get_gateway_service = MagicMock(
            return_value=request.getfixturevalue(mock_service_fixture)
        )

        harness.charm._get_gateway_service = mock_get_gateway_service
        assert harness.charm._is_gateway_service_up is is_gateway_up

    @pytest.mark.parametrize(
        "mock_service_fixture, gateway_address",
        [
            # Pass fixtures by their names
            ("mock_nodeport_service", None),
            ("mock_clusterip_service", "10.10.10.10"),
            ("mock_loadbalancer_hostname_service", "test.com"),
            ("mock_loadbalancer_ip_service", "127.0.0.1"),
            ("mock_loadbalancer_hostname_service_not_ready", None),
            ("mock_loadbalancer_ip_service_not_ready", None),
        ],
    )
    def test_get_gateway_address_from_svc(
        self,
        mock_service_fixture,
        gateway_address,
        harness,
        request,
    ):
        """Test that the charm._gateway_address correctly returns gateway service IP/hostname."""
        mock_service = request.getfixturevalue(mock_service_fixture)

        assert _get_gateway_address_from_svc(svc=mock_service) is gateway_address

    def test_get_ingress_auth_data(self, harness):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        returned_data = add_ingress_auth_to_harness(harness)

        ingress_auth_data = harness.charm._get_ingress_auth_data()

        assert len(ingress_auth_data) == 1
        assert list(ingress_auth_data.values())[0] == returned_data['data']

    def test_get_ingress_auth_data_empty(self, harness):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        ingress_auth_data = harness.charm._get_ingress_auth_data()

        assert len(ingress_auth_data) == 0

    def test_get_ingress_auth_data_too_many_relations(self, harness):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        add_ingress_auth_to_harness(harness, other_app="other1")
        add_ingress_auth_to_harness(harness, other_app="other2")

        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._get_ingress_auth_data()

        assert "Multiple ingress-auth" in err.value.msg

    def test_get_ingress_auth_data_waiting_on_version(self, harness):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        harness.add_relation("ingress-auth", "other")

        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._get_ingress_auth_data()

        assert "versions not found" in err.value.msg

    @patch("charm.KubernetesResourceHandler", return_value=MagicMock())
    def test_reconcile_ingress_auth(self, mocked_kubernetes_resource_handler_class, harness):
        """Tests that the _reconcile_ingress_auth helper succeeds when expected."""
        mocked_krh = mocked_kubernetes_resource_handler_class.return_value
        ingress_auth_data = {
            'port': 1234,
            'service': 'some-service',
            'request_headers': 'header1',
            'response_headers': 'header2',
        }
        harness.begin()

        harness.charm._reconcile_ingress_auth(ingress_auth_data)

        mocked_krh.apply.assert_called_once()

    @patch("charm._remove_envoyfilter")
    @patch("charm.KubernetesResourceHandler", return_value=MagicMock())
    def test_reconcile_ingress_auth_no_auth(self, _mocked_kubernetes_resource_handler_class, mocked_remove_envoyfilter, harness):
        """Tests that the _reconcile_ingress_auth removes the EnvoyFilter when expected."""
        ingress_auth_data = {}
        harness.begin()

        harness.charm._reconcile_ingress_auth(ingress_auth_data)

        mocked_remove_envoyfilter.assert_called_once()

    @patch("charm.Client", return_value=MagicMock())
    def test_remove_envoyfilter(self, mocked_lightkube_client_class):
        """Test that _renove_envoyfilter works when expected."""
        name = "test"
        namespace = "test-namespace"
        mocked_lightkube_client = mocked_lightkube_client_class.return_value

        _remove_envoyfilter(name, namespace)

        mocked_lightkube_client.delete.assert_called_once()

    @pytest.mark.parametrize(
        "error_code, context_raised",
        [
            (999, pytest.raises(ApiError)),  # Generic ApiErrors are raised
            (404, does_not_raise())  # 404 errors are ignored
        ]
    )
    @patch("charm.Client", return_value=MagicMock())
    def test_remove_envoyfilter_error_handling(self, mocked_lightkube_client_class, error_code, context_raised):
        """Test that _renove_envoyfilter handles errors as expected."""
        name = "test"
        namespace = "test-namespace"
        mocked_lightkube_client = mocked_lightkube_client_class.return_value
        mocked_lightkube_client.delete.side_effect = FakeApiError(error_code)

        with context_raised:
            _remove_envoyfilter(name, namespace)


    @pytest.mark.parametrize(
        "left, right, expected",
        [
            (True, False, True),
            (False, True, True),
            (True, True, False),
            (False, False, False),
        ]
    )
    def test_xor(self, left, right, expected):
        """Test that the xor helper function works as expected."""
        assert _xor(left, right) is expected


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


@pytest.fixture()
def mock_nodeport_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{}]}},
            "spec": {"type": "NodePort", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_clusterip_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{}]}},
            "spec": {"type": "ClusterIP", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_ip_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{"ip": "127.0.0.1"}]}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_hostname_service():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": [{"hostname": "test.com"}]}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_ip_service_not_ready():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": []}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


@pytest.fixture()
def mock_loadbalancer_hostname_service_not_ready():
    mock_nodeport_service = codecs.from_dict(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "status": {"loadBalancer": {"ingress": []}},
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.10"},
        }
    )
    return mock_nodeport_service


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
def add_data_to_sdi_relation(harness: Harness, rel_id: str, other: str, data: Optional[dict] = None, supported_versions: str = "- v1") -> None:
    """Add data to the an SDI-backed relation."""
    if data is None:
        data = {}

    harness.update_relation_data(
        rel_id,
        other,
        {"_supported_versions": supported_versions, "data": yaml.dump(data)},
    )


def add_ingress_auth_to_harness(harness: Harness, other_app="other") -> dict:
    """Relates a new app and unit to the ingress-auth relation.

    Returns dict of:
    * other (str): The name of the other app
    * other_unit (str): The name of the other unit
    * rel_id (int): The relation id
    * data (dict): The relation data put to the relation
    """
    other_unit=f"{other_app}/0"
    rel_id = harness.add_relation("ingress-auth", other_app)

    harness.add_relation_unit(rel_id, other_unit)
    data = {
        "service": "service-name",
        "port": 6666,
        "allowed-request-headers": ["foo"],
        "allowed-response-headers": ["bar"],
    }
    add_data_to_sdi_relation(harness, rel_id, other_app, data)

    return {
        "other": other_app,
        "other_unit": other_unit,
        "rel_id": rel_id,
        "data": data,
    }


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
