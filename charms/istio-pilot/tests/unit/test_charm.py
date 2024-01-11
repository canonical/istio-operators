import logging
from contextlib import nullcontext as does_not_raise
from typing import Optional
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest
import tenacity
import yaml
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.mocking import FakeApiError
from lightkube import codecs
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Secret
from ops.charm import (
    ActionEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationCreatedEvent,
    RelationJoinedEvent,
)
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    SecretNotFoundError,
    WaitingStatus,
)
from ops.testing import Harness

from charm import (
    GATEWAY_PORTS,
    TLS_SECRET_LABEL,
    Operator,
    _get_gateway_address_from_svc,
    _remove_envoyfilter,
    _validate_upgrade_version,
    _wait_for_update_rollout,
    _xor,
)
from istioctl import IstioctlError

GATEWAY_LIGHTKUBE_RESOURCE = create_namespaced_resource(
    group="networking.istio.io", version="v1alpha3", kind="Gateway", plural="gateways"
)

VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE = create_namespaced_resource(
    group="networking.istio.io",
    version="v1alpha3",
    kind="VirtualService",
    plural="virtualservices",
)


@pytest.fixture()
def all_operator_reconcile_handlers_mocked(mocker):
    mocked = {
        "_check_leader": mocker.patch("charm.Operator._check_leader"),
        "_handle_istio_pilot_relation": mocker.patch(
            "charm.Operator._handle_istio_pilot_relation"
        ),
        "_get_ingress_auth_data": mocker.patch("charm.Operator._get_ingress_auth_data"),
        "_reconcile_ingress_auth": mocker.patch("charm.Operator._reconcile_ingress_auth"),
        "_reconcile_gateway": mocker.patch("charm.Operator._reconcile_gateway"),
        "_remove_gateway": mocker.patch("charm.Operator._remove_gateway"),
        "_send_gateway_info": mocker.patch("charm.Operator._send_gateway_info"),
        "_get_ingress_data": mocker.patch("charm.Operator._get_ingress_data"),
        "_reconcile_ingress": mocker.patch("charm.Operator._reconcile_ingress"),
        "_report_handled_errors": mocker.patch("charm.Operator._report_handled_errors"),
    }
    yield mocked


@pytest.fixture()
def kubernetes_resource_handler_with_client(mocker):
    mocked_client = MagicMock()

    class KubernetesResourceHandlerWithClient(KubernetesResourceHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._lightkube_client = mocked_client

    mocker.patch("charm.KubernetesResourceHandler", new=KubernetesResourceHandlerWithClient)
    yield KubernetesResourceHandlerWithClient, mocked_client


@pytest.fixture()
def kubernetes_resource_handler_with_client_and_existing_gateway(
    kubernetes_resource_handler_with_client,
):
    """Yields a KubernetesResourceHandlerWithClient with a mocked client and a mocked Gateway.

    The mocked Gateway is returned from lightkube_client.list() to simulate finding a gateway
    during reconciliation or deletion.
    """
    mocked_krh_class, mocked_lightkube_client = kubernetes_resource_handler_with_client

    # Mock a previously existing resource so we have something to remove
    existing_gateway_name = "my-old-gateway"
    existing_gateway_namespace = "my-namespace"
    mocked_lightkube_client.list.return_value = [
        GATEWAY_LIGHTKUBE_RESOURCE(
            metadata=ObjectMeta(name=existing_gateway_name, namespace=existing_gateway_namespace)
        )
    ]

    yield mocked_krh_class, mocked_lightkube_client, existing_gateway_name


@pytest.fixture()
def kubernetes_resource_handler_with_client_and_existing_virtualservice(
    kubernetes_resource_handler_with_client,
):
    """Yields a KubernetesResourceHandlerWithClient with a mocked client and a mocked VS.

    The mocked VirtualService is returned from lightkube_client.list() to simulate finding a VS
    during reconciliation or deletion.
    """
    mocked_krh_class, mocked_lightkube_client = kubernetes_resource_handler_with_client

    # Mock a previously existing resource so we have something to remove
    existing_vs_name = "my-old-vs"
    existing_vs_namespace = f"{existing_vs_name}-namespace"
    mocked_lightkube_client.list.return_value = [
        VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE(
            metadata=ObjectMeta(name=existing_vs_name, namespace=existing_vs_namespace)
        )
    ]

    yield mocked_krh_class, mocked_lightkube_client, existing_vs_name


def raise_apierror_with_code_400(*args, **kwargs):
    raise FakeApiError(400)


def raise_apierror_with_code_404(*args, **kwargs):
    raise FakeApiError(404)


class TestCharmEvents:
    """Test cross-cutting charm behavior.

    This test class includes any end-to-end style unit tests (eg: that use Juju's events and test
    their handling, etc).
    """

    def test_event_observing(self, harness, mocker, mocked_cert_subject):
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
        assert mocked_reconcile.call_count == 2
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationCreatedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationJoinedEvent)
        mocked_reconcile.reset_mock()

        exercise_relation(harness, "istio-pilot")
        assert mocked_reconcile.call_count == 3
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationCreatedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationJoinedEvent)
        assert isinstance(mocked_reconcile.call_args_list[2][0][0], RelationChangedEvent)
        mocked_reconcile.reset_mock()

        exercise_relation(harness, "ingress")
        assert mocked_reconcile.call_count == 2
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationChangedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationBrokenEvent)
        mocked_reconcile.reset_mock()

        exercise_relation(harness, "ingress-auth")
        assert mocked_reconcile.call_count == 4
        assert isinstance(mocked_reconcile.call_args_list[0][0][0], RelationCreatedEvent)
        assert isinstance(mocked_reconcile.call_args_list[1][0][0], RelationJoinedEvent)
        assert isinstance(mocked_reconcile.call_args_list[2][0][0], RelationChangedEvent)
        assert isinstance(mocked_reconcile.call_args_list[3][0][0], RelationBrokenEvent)
        mocked_reconcile.reset_mock()

    def test_not_leader(self, harness, mocked_cert_subject):
        """Assert that the charm does not perform any actions when not the leader."""
        harness.set_leader(False)
        harness.begin()
        harness.charm.on.config_changed.emit()
        assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")

    @patch("charm.Operator._remove_gateway")
    def test_ingress_auth_and_gateway(
        self,
        mocked_remove_gateway,
        harness,
        mocker,
        mocked_lightkube_client,
        kubernetes_resource_handler_with_client,
    ):
        """Charm e2e test that asserts that we correctly manage our Gateway with/without Auth.

        Asserts that we:
        * create a gateway on a config_changed
        * remove the gateway on an incomple ingress-auth relation
        * recreate the gateway and create an EnvoyFilter on a complete ingress-auth relation

        """
        krh_class, krh_lightkube_client = kubernetes_resource_handler_with_client

        model_name = "my-model"
        gateway_name = "my-gateway"
        harness.set_leader(True)
        harness.set_model_name(model_name)
        harness.update_config({"default-gateway": gateway_name})

        harness.begin()
        mocker.patch("charm.Operator.upgrade_charm")

        harness.charm.log = MagicMock()

        # Do a reconcile
        harness.charm.on.config_changed.emit()

        # Assert we have created a gateway during reconcile
        assert krh_lightkube_client.apply.call_count == 1
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, gateway_name, model_name
        )
        krh_lightkube_client.reset_mock()

        # Add "broken" ingress_auth (empty data) and check that we remove the gateway
        rel_id = harness.add_relation("ingress-auth", "other")
        add_data_to_sdi_relation(harness, rel_id, "other", {})
        mocked_remove_gateway.assert_called_once
        mocked_remove_gateway.reset_mock()

        assert harness.charm.model.unit.status == WaitingStatus(
            "Execution handled 1 errors.  See logs for details."
        )
        assert (
            "Handled error 0/1: WaitingStatus('Waiting for the auth provider data.')"
            in harness.charm.log.info.call_args.args
        )

        # Remove ingress_auth relation and check that we re-add the gateway
        harness.remove_relation(rel_id)
        assert krh_lightkube_client.apply.call_count == 1
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, gateway_name, model_name
        )
        krh_lightkube_client.reset_mock()

        # Add complete ingress_auth data and check that we created the gateway and envoyfilter
        envoyfilter_name = f"{harness.model.app.name}-authn-filter"
        add_ingress_auth_to_harness(harness, "other")
        assert krh_lightkube_client.apply.call_count == 2
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, gateway_name, model_name
        )
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, envoyfilter_name, model_name
        )
        assert_envoyfilter_applied_to_all_gateway_ports(
            krh_lightkube_client, envoyfilter_name, model_name
        )

    @patch("charm.Operator._handle_istio_pilot_relation")
    def test_ingress_relation(
        self,
        mocked_handle_istio_pilot_relation,
        harness,
        mocker,
        mocked_lightkube_client,
        kubernetes_resource_handler_with_client,
    ):
        """Charm e2e test that asserts that we correctly manage ingress relation.

        Asserts that we:
        * create a gateway on a config_changed
        * create a single VirtualService when we add one related app to `ingress`
        * create two VirtualServices when we add another related app to `ingress`
        * create the VirtualServices for ingress even when other non-fatal errors occur

        """
        krh_class, krh_lightkube_client = kubernetes_resource_handler_with_client

        model_name = "my-model"
        gateway_name = "my-gateway"
        harness.set_leader(True)
        harness.set_model_name(model_name)
        harness.update_config({"default-gateway": gateway_name})

        harness.begin()
        mocker.patch("charm.Operator.upgrade_charm")

        # Do a reconcile
        harness.charm.on.config_changed.emit()

        # Assert we have created a gateway during reconcile
        assert krh_lightkube_client.apply.call_count == 1
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, gateway_name, model_name
        )
        krh_lightkube_client.reset_mock()

        # Add ingress relation and check it results in VirtualServices being created
        ingress_app1 = "other1"
        add_ingress_to_harness(harness, ingress_app1)
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, ingress_app1, model_name
        )
        krh_lightkube_client.reset_mock()

        # Add another ingress relation and check it results in 2 VirtualServices being created
        ingress_app2 = "other2"
        add_ingress_to_harness(harness, ingress_app2)
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, ingress_app1, model_name
        )
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, ingress_app2, model_name
        )
        krh_lightkube_client.reset_mock()

        # After everything, the unit should be active
        assert harness.charm.model.unit.status == ActiveStatus()

        # If we "break" part of the charm, we should still create the VirtualServices but the charm
        # is not active
        mocked_handle_istio_pilot_relation.side_effect = ErrorWithStatus(
            "Test error", BlockedStatus
        )
        harness.charm.on.config_changed.emit()
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, ingress_app1, model_name
        )
        assert is_lightkube_resource_in_call_args_list(
            krh_lightkube_client.apply.call_args_list, ingress_app2, model_name
        )
        assert isinstance(harness.charm.model.unit.status, BlockedStatus)
        assert "handled 1 error" in harness.charm.model.unit.status.message

    def test_istio_pilot_relation(
        self, harness, mocker, mocked_lightkube_client, kubernetes_resource_handler_with_client
    ):
        """Charm e2e test that asserts we correctly broadcast data on the istio-pilot relation."""
        krh_class, krh_lightkube_client = kubernetes_resource_handler_with_client

        model_name = "my-model"
        gateway_name = "my-gateway"
        harness.set_leader(True)
        harness.set_model_name(model_name)
        harness.update_config({"default-gateway": gateway_name})

        harness.begin()
        mocker.patch("charm.Operator.upgrade_charm")

        # Do a reconcile
        harness.charm.on.config_changed.emit()

        # Add istio-pilot relation and check it posts data correctly
        istio_pilot_relation_info = add_istio_pilot_to_harness(harness, "other")
        expected_service_name = f"istiod.{model_name}.svc"
        actual_service_name = yaml.safe_load(
            harness.get_relation_data(istio_pilot_relation_info["rel_id"], harness.model.app)[
                "data"
            ]
        )["service-name"]
        assert actual_service_name == expected_service_name
        krh_lightkube_client.reset_mock()

    @patch("charm.Operator._is_gateway_service_up", new_callable=PropertyMock)
    def test_gateway_info_relation(
        self,
        mocked_is_gateway_service_up,
        harness,
        mocker,
        mocked_lightkube_client,
        kubernetes_resource_handler_with_client,
    ):
        """Charm e2e test that asserts we correctly broadcast data on the gateway-info relation."""
        # Arrange
        model_name = "my-model"
        gateway_name = "my-gateway"
        harness.set_leader(True)
        harness.set_model_name(model_name)
        harness.update_config({"default-gateway": gateway_name})

        mocked_is_gateway_service_up.return_value = True

        harness.begin()
        mocker.patch("charm.Operator.upgrade_charm")

        # Act and assert
        # Add gateway-info relation and check it posts data correctly
        gateway_info_relation_info = add_gateway_info_to_harness(harness, "other")
        actual_gateway_name = harness.get_relation_data(
            gateway_info_relation_info["rel_id"], harness.model.app
        )["gateway_name"]
        assert actual_gateway_name == gateway_name
        actual_gateway_up = harness.get_relation_data(
            gateway_info_relation_info["rel_id"], harness.model.app
        )["gateway_up"]
        assert actual_gateway_up == "true"
        assert harness.charm.model.unit.status == ActiveStatus()

    @pytest.mark.parametrize(
        "current_cni_bin_dir, current_cni_conf_dir, new_cni_bin_dir, new_cni_conf_dir, expected_output",  # noqa
        [
            ("current-bin", "current-conf", "current-bin", "current-conf", False),
            ("current-bin", "current-conf", "new-bin", "new-conf", True),
            ("current-bin", "current-conf", "current-bin", "new-conf", True),
            ("current-bin", "", "new-bin", "", True),
            ("", "current-conf", "", "new-conf", True),
            ("", "", "", "", False),
        ],
    )
    def test_cni_config_changed(
        self,
        current_cni_bin_dir,
        current_cni_conf_dir,
        new_cni_bin_dir,
        new_cni_conf_dir,
        expected_output,
        harness,
        mocked_lightkube_client,
    ):
        model_name = "my-model"
        harness.set_leader(True)
        harness.set_model_name(model_name)

        # Set peer relation
        rel_id = harness.add_relation("peers", "istio-pilot")
        harness.add_relation_unit(rel_id, "istio-pilot/0")

        # Set up relation data with current config values
        harness.update_relation_data(rel_id, "istio-pilot/0", {"cni-bin-dir": current_cni_bin_dir})
        harness.update_relation_data(
            rel_id, "istio-pilot/0", {"cni-conf-dir": current_cni_conf_dir}
        )

        # Update config values
        harness.update_config({"cni-bin-dir": new_cni_bin_dir})
        harness.update_config({"cni-conf-dir": new_cni_conf_dir})
        harness.begin()
        actual_output = harness.charm._cni_config_changed()
        assert actual_output is expected_output

    def test_cni_config_changed_no_peer_relation(self, harness, mocked_lightkube_client):
        model_name = "my-model"
        harness.set_leader(True)
        harness.set_model_name(model_name)

        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm._cni_config_changed()


class TestCharmHelpers:
    """Directly test charm helpers and private methods."""

    def test_reconcile_handling_nonfatal_errors(
        self,
        harness,
        mocker,
        all_operator_reconcile_handlers_mocked,
        mocked_cert_subject,
    ):
        """Test does a charm e2e simulation of a reconcile loop which handles non-fatal errors."""
        # Arrange
        mocks = all_operator_reconcile_handlers_mocked
        mocks["_handle_istio_pilot_relation"].side_effect = ErrorWithStatus(
            "_handle_istio_pilot_relation", BlockedStatus
        )
        mocks["_reconcile_gateway"].side_effect = ErrorWithStatus(
            "_reconcile_gateway", BlockedStatus
        )
        mocks["_send_gateway_info"].side_effect = ErrorWithStatus(
            "_send_gateway_info", BlockedStatus
        )
        mocks["_get_ingress_data"].side_effect = ErrorWithStatus(
            "_get_ingress_data", BlockedStatus
        )

        harness.begin()
        mocker.patch("charm.Operator.upgrade_charm")

        # Act
        harness.charm.reconcile("event")

        # Assert
        mocks["_report_handled_errors"].assert_called_once()
        assert len(mocks["_report_handled_errors"].call_args.kwargs["errors"]) == 4

    def test_reconcile_not_leader(
        self,
        harness,
        mocked_cert_subject,
    ):
        """Assert that the reconcile handler does not perform any actions when not the leader."""
        harness.set_leader(False)
        harness.begin()
        harness.charm.reconcile("mock event")
        assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")

    @pytest.mark.parametrize(
        "mock_service_fixture, gateway_address",
        [
            # Pass fixtures by their names
            ("mock_nodeport_service", "10.10.10.10"),
            ("mock_clusterip_service", "10.10.10.11"),
            ("mock_loadbalancer_hostname_service", "test.com"),
            ("mock_loadbalancer_ip_service", "127.0.0.1"),
        ],
    )
    def test_cert_subject_returns_no_config(
        self,
        mock_service_fixture,
        gateway_address,
        harness,
        mocked_lightkube_client,
        request,
    ):
        """Assert the property returns the ingress gateway address."""
        harness.begin()

        mock_get_gateway_service = MagicMock(
            return_value=request.getfixturevalue(mock_service_fixture)
        )

        harness.charm._get_gateway_service = mock_get_gateway_service

        assert harness.charm._cert_subject == gateway_address

    def test_cert_subject_returns_with_config(self, harness, mocked_lightkube_client):
        """Assert the property returns the domain name config value when set."""
        harness.begin()

        expected_domain_name = "test.com"
        harness.update_config(
            {
                "csr-domain-name": expected_domain_name,
            }
        )
        assert harness.charm._cert_subject == expected_domain_name

    def test_cert_subject_none(self, harness, mocked_lightkube_client):
        """Assert returns None when no csr-domain-name/gateway service address is set in place."""
        harness.begin()
        assert harness.charm._cert_subject is None

    @pytest.mark.parametrize(
        "cert_handler_enabled, ssl_cert, ssl_key, expected_port, expected_context",
        [
            (False, "", "", GATEWAY_PORTS["http"], does_not_raise()),
            (True, "x", "y", GATEWAY_PORTS["https"], does_not_raise()),
            (True, "x", "", None, pytest.raises(ErrorWithStatus)),
            (True, "", "y", None, pytest.raises(ErrorWithStatus)),
        ],
    )
    def test_gateway_port(
        self,
        cert_handler_enabled,
        ssl_cert,
        ssl_key,
        expected_port,
        expected_context,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the gateway_port selection works as expected."""
        harness.begin()

        harness.charm._cert_handler = MagicMock()
        harness.charm._cert_handler.enabled = cert_handler_enabled
        harness.charm._cert_handler.cert = ssl_cert
        harness.charm._cert_handler.key = ssl_key

        with expected_context:
            gateway_port = harness.charm._gateway_port
            assert gateway_port == expected_port

    @pytest.mark.parametrize(
        "lightkube_client_get_side_effect, expected_is_up, context_raised",
        [
            (None, True, does_not_raise()),
            (raise_apierror_with_code_404, False, does_not_raise()),
            (raise_apierror_with_code_400, None, pytest.raises(ApiError)),
            (ValueError, None, pytest.raises(ValueError)),
        ],
    )
    def test_is_gateway_object_up(
        self,
        lightkube_client_get_side_effect,
        expected_is_up,
        context_raised,
        harness,
        mocked_lightkube_client,
        mocked_cert_subject,
    ):
        """Tests whether _is_gateway_object_up returns as expected."""
        mocked_lightkube_client.get.side_effect = lightkube_client_get_side_effect

        harness.begin()

        with context_raised:
            actual_is_up = harness.charm._is_gateway_object_up
            assert actual_is_up == expected_is_up

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
    def test_is_gateway_service_up(
        self,
        mock_service_fixture,
        is_gateway_up,
        harness,
        request,
        mocked_cert_subject,
    ):
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
            ("mock_nodeport_service", "10.10.10.10"),
            ("mock_clusterip_service", "10.10.10.11"),
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
        mocked_cert_subject,
    ):
        """Test that the charm._gateway_address correctly returns gateway service IP/hostname."""
        mock_service = request.getfixturevalue(mock_service_fixture)

        assert _get_gateway_address_from_svc(svc=mock_service) is gateway_address

    def test_get_ingress_auth_data(
        self,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        returned_data = add_ingress_auth_to_harness(harness)

        ingress_auth_data = harness.charm._get_ingress_auth_data("not-relation-broken-event")

        assert ingress_auth_data == returned_data["data"]

    def test_get_ingress_auth_data_empty(
        self,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        ingress_auth_data = harness.charm._get_ingress_auth_data("not-relation-broken-event")

        assert len(ingress_auth_data) == 0

    def test_get_ingress_auth_data_empty_error(self, harness, mocked_lightkube_client):
        """Tests that the _get_ingress_auth_data helper returns the correct relation data."""
        harness.begin()
        rel_id = harness.add_relation("ingress-auth", "other")
        add_data_to_sdi_relation(harness, rel_id, "other", {})
        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._get_ingress_auth_data("not-relation-broken-event")

        assert err.value.status_type.name == "waiting"
        assert "Waiting for the auth provider data." == err.value.msg

    def test_get_ingress_auth_data_too_many_relations(
        self,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the _get_ingress_auth_data helper raises on too many relations data."""
        harness.begin()
        add_ingress_auth_to_harness(harness, other_app="other1")
        add_ingress_auth_to_harness(harness, other_app="other2")

        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._get_ingress_auth_data("not-relation-broken-event")

        assert "Multiple ingress-auth" in err.value.msg

    def test_get_ingress_auth_data_waiting_on_version(
        self,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the _get_ingress_auth_data helper raises on incomplete data."""
        harness.begin()
        harness.add_relation("ingress-auth", "other")

        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._get_ingress_auth_data("not-relation-broken-event")

        assert "versions not found" in err.value.msg

    def test_get_ingress_data(self, harness, mocked_cert_subject):
        """Tests that the _get_ingress_data helper returns the correct relation data."""
        harness.begin()
        relation_info = [
            add_ingress_to_harness(harness, "other1"),
            add_ingress_to_harness(harness, "other2"),
        ]

        event = "not-a-relation-broken-event"

        ingress_data = harness.charm._get_ingress_data(event)

        assert len(ingress_data) == len(relation_info)
        for i, this_relation_info in enumerate(relation_info):
            this_relation = harness.model.get_relation("ingress", i)
            assert ingress_data[(this_relation, this_relation.app)] == this_relation_info["data"]

    def test_get_ingress_data_for_broken_event(self, harness, mocked_cert_subject):
        """Tests that _get_ingress_data helper returns the correct for a RelationBroken event."""
        harness.begin()
        relation_info = [
            add_ingress_to_harness(harness, "other0"),
            add_ingress_to_harness(harness, "other1"),
        ]

        # Check for data while pretending this is a RelationBrokenEvent for relation[1] of the
        # above relations.
        mock_relation_broken_event = MagicMock(spec=RelationBrokenEvent)
        mock_relation_broken_event.relation = harness.model.get_relation("ingress", 1)
        mock_relation_broken_event.app = harness.model.get_relation("ingress", 1).app

        ingress_data = harness.charm._get_ingress_data(mock_relation_broken_event)

        assert len(ingress_data) == 1
        this_relation = harness.model.get_relation("ingress", 0)
        assert ingress_data[(this_relation, this_relation.app)] == relation_info[0]["data"]

    def test_get_ingress_data_for_broken_event_none_event_app(self, harness, mocked_cert_subject):
        """Tests _get_ingress_data helper logs on RelationBroken event when event.app is None."""
        harness.begin()
        # Check for data while pretending this is a RelationBrokenEvent for relation[1] of the
        # above relations.
        mock_relation_broken_event = MagicMock(spec=RelationBrokenEvent)
        mock_relation_broken_event.relation = harness.model.get_relation("ingress", 1)
        mock_relation_broken_event.app = None

        # Mock the logger
        harness.charm.log = MagicMock()

        harness.charm._get_ingress_data(mock_relation_broken_event)
        assert harness.charm.log.info.call_count == 1

    def test_get_ingress_data_empty(self, harness, mocked_cert_subject):
        """Tests that the _get_ingress_data helper returns the correct empty relation data."""
        harness.begin()
        event = "not-a-relation-broken-event"

        ingress_data = harness.charm._get_ingress_data(event)

        assert len(ingress_data) == 0

    def test_get_ingress_data_waiting_on_version(self, harness, mocked_cert_subject):
        """Tests that the _get_ingress_data helper raises on incomplete data."""
        harness.begin()
        harness.add_relation("ingress", "other")

        event = "not-a-relation-broken-event"

        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._get_ingress_data(event)

        assert "versions not found" in err.value.msg

    @pytest.mark.parametrize(
        "related_applications",
        [
            ([]),  # No related applications
            (["other1"]),  # A single related application
            (["other1", "other2", "other3"]),  # Multiple related applications
        ],
    )
    def test_handle_istio_pilot_relation(self, related_applications, harness, mocked_cert_subject):
        """Tests that the handle_istio_pilot_relation helper works as expected."""
        # Assert
        # Must be leader because we write to the application part of the relation data
        model_name = "some-model"
        expected_data = {
            "service-name": f"istiod.{model_name}.svc",
            "service-port": "15012",
        }

        harness.set_leader(True)
        harness.set_model_name(model_name)

        relation_info = [
            add_istio_pilot_to_harness(harness, other_app=name) for name in related_applications
        ]
        harness.begin()

        # Act
        harness.charm._handle_istio_pilot_relation()

        # Assert on the relation data
        # The correct number of relations exist
        assert len(harness.model.relations["istio-pilot"]) == len(relation_info)

        # For each relation, the relation data is correct
        for this_relation_info in relation_info:
            actual_data = yaml.safe_load(
                harness.get_relation_data(this_relation_info["rel_id"], "istio-pilot")["data"]
            )
            assert expected_data == actual_data

    def test_handle_istio_pilot_relation_waiting_on_version(self, harness, mocked_cert_subject):
        """Tests that the _handle_istio_pilot_relation helper raises on incomplete data."""
        # Arrange
        harness.add_relation("istio-pilot", "other")
        harness.begin()

        # Act and assert
        with pytest.raises(ErrorWithStatus) as err:
            harness.charm._handle_istio_pilot_relation()
        assert "versions not found" in err.value.msg

    def test_reconcile_gateway_with_tls(
        self,
        harness,
        kubernetes_resource_handler_with_client_and_existing_gateway,
        mocked_cert_subject,
    ):
        """Test that reconcile_gateway works with TLS configuration."""

        # Arrange
        (
            mocked_krh_class,
            mocked_lightkube_client,
            existing_gateway_name,
        ) = kubernetes_resource_handler_with_client_and_existing_gateway

        default_gateway = "my-gateway"
        harness.update_config(
            {
                "default-gateway": default_gateway,
            }
        )

        harness.begin()
        harness.charm._cert_handler = MagicMock()
        harness.charm._cert_handler.enabled = True
        harness.charm._cert_handler.cert = "some-cert"
        harness.charm._cert_handler.key = "some-key"

        # Act
        harness.charm._reconcile_gateway()

        servers_dict = mocked_lightkube_client.apply.call_args.kwargs["obj"].spec["servers"][0][
            "port"
        ]
        assert servers_dict["name"] == "https"
        assert servers_dict["protocol"] == "HTTPS"

    def test_reconcile_gateway(
        self,
        harness,
        kubernetes_resource_handler_with_client_and_existing_gateway,
        mocked_cert_subject,
    ):
        """Tests that reconcile_gateway works when expected."""
        # Arrange
        (
            mocked_krh_class,
            mocked_lightkube_client,
            existing_gateway_name,
        ) = kubernetes_resource_handler_with_client_and_existing_gateway

        default_gateway = "my-gateway"
        harness.update_config(
            {
                "default-gateway": default_gateway,
            }
        )

        harness.begin()

        # Act
        harness.charm._reconcile_gateway()

        # Assert
        # We've mocked the list method very broadly.  Ensure we only get called the time we expect
        assert mocked_lightkube_client.list.call_count == 2
        created_resources = [
            args_list.args[0] for args_list in mocked_lightkube_client.list.call_args_list
        ]
        assert GATEWAY_LIGHTKUBE_RESOURCE in created_resources
        assert Secret in created_resources

        # Assert that we tried to remove the old gateway
        assert mocked_lightkube_client.delete.call_args.kwargs["name"] == existing_gateway_name

        # Assert that we tried to create our gateway
        assert mocked_lightkube_client.apply.call_count == 1
        assert (
            mocked_lightkube_client.apply.call_args.kwargs["obj"].metadata.name == default_gateway
        )

    @pytest.mark.parametrize(
        "related_applications",
        [
            ([]),  # No related applications
            (["other1"]),  # A single related application
            (["other1", "other2", "other3"]),  # Multiple related applications
        ],
    )
    def test_reconcile_ingress(
        self,
        related_applications,
        harness,
        kubernetes_resource_handler_with_client_and_existing_virtualservice,
        mocked_cert_subject,
    ):
        """Tests that _reconcile_ingress succeeds as expected.

        Asserts that previous VirtualServices are removed and that the any new ones are created.
        """
        # Arrange
        (
            mocked_krh_class,
            mocked_lightkube_client,
            existing_virtualservice_name,
        ) = kubernetes_resource_handler_with_client_and_existing_virtualservice

        harness.begin()
        relation_info = [add_ingress_to_harness(harness, name) for name in related_applications]

        event = "not-a-relation-broken-event"
        ingress_data = harness.charm._get_ingress_data(event)

        # Act
        harness.charm._reconcile_ingress(ingress_data)

        # Assert
        # We've mocked the list method very broadly.  Ensure we only get called the time we expect
        assert mocked_lightkube_client.list.call_count == 1
        assert (
            mocked_lightkube_client.list.call_args_list[0].args[0]
            == VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE
        )

        # Assert that we tried to remove the old VirtualService
        assert (
            mocked_lightkube_client.delete.call_args.kwargs["name"] == existing_virtualservice_name
        )

        # Assert that we tried to create a VirtualService for each related application
        assert mocked_lightkube_client.apply.call_count == len(relation_info)
        for i, this_relation_info in enumerate(relation_info):
            assert (
                mocked_lightkube_client.apply.call_args_list[i].kwargs["obj"].metadata.name
                == this_relation_info["data"]["service"]
            )

    def test_reconcile_ingress_update_existing_virtualservice(
        self,
        harness,
        kubernetes_resource_handler_with_client_and_existing_virtualservice,
        mocked_cert_subject,
    ):
        """Tests that _reconcile_ingress works as expected when there are no related applications.

        Asserts that previous VirtualServices are removed and that no new ones are created.
        """
        # Arrange
        (
            mocked_krh_class,
            mocked_lightkube_client,
            existing_virtualservice_name,
        ) = kubernetes_resource_handler_with_client_and_existing_virtualservice

        # Name this model the same as the existing VirtualService's namespace.  This means when
        # we try to reconcile, we will see that existing VirtualService as the same as the desired
        # one, and thus we try to update it instead of delete it
        harness.set_model_name(f"{existing_virtualservice_name}-namespace")
        harness.begin()

        # Add a VirtualService that has the same name/namespace as the existing one
        relation_info = [add_ingress_to_harness(harness, existing_virtualservice_name)]
        event = "not-a-relation-broken-event"
        ingress_data = harness.charm._get_ingress_data(event)

        # Act
        harness.charm._reconcile_ingress(ingress_data)

        # Assert
        # We've mocked the list method very broadly.  Ensure we only get called the time we expect
        assert mocked_lightkube_client.list.call_count == 1
        assert (
            mocked_lightkube_client.list.call_args_list[0].args[0]
            == VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE
        )

        # Assert that we DO NOT try to remove the old VirtualService
        assert mocked_lightkube_client.delete.call_count == 0

        # Assert that we tried to apply our VirtualService to update it
        assert mocked_lightkube_client.apply.call_count == len(relation_info)
        for i, this_relation_info in enumerate(relation_info):
            assert (
                mocked_lightkube_client.apply.call_args_list[i].kwargs["obj"].metadata.name
                == this_relation_info["data"]["service"]
            )

    @patch("charm.KubernetesResourceHandler", return_value=MagicMock())
    def test_reconcile_ingress_auth(
        self,
        mocked_kubernetes_resource_handler_class,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the _reconcile_ingress_auth helper succeeds when expected."""
        mocked_krh = mocked_kubernetes_resource_handler_class.return_value
        ingress_auth_data = {
            "port": 1234,
            "service": "some-service",
            "allowed-request-headers": "header1",
            "allowed-response-headers": "header2",
        }
        harness.begin()

        harness.charm._reconcile_ingress_auth(ingress_auth_data)

        mocked_krh.apply.assert_called_once()

    @patch("charm._remove_envoyfilter")
    @patch("charm.KubernetesResourceHandler", return_value=MagicMock())
    def test_reconcile_ingress_auth_no_auth(
        self,
        _mocked_kubernetes_resource_handler_class,
        mocked_remove_envoyfilter,
        harness,
        mocked_cert_subject,
    ):
        """Tests that the _reconcile_ingress_auth removes the EnvoyFilter when expected."""
        ingress_auth_data = {}
        harness.begin()

        harness.charm._reconcile_ingress_auth(ingress_auth_data)

        mocked_remove_envoyfilter.assert_called_once()

    @patch("charm._remove_envoyfilter")
    @patch("charm.KubernetesResourceHandler", return_value=MagicMock())
    @patch("charm.Operator._cni_config_changed", return_value=True)
    @patch("charm.Istioctl", return_value=MagicMock())
    def test_reconcile_cni_config_changed(
        self,
        mocked_istioctl_class,
        mocked_cni_config_changed,
        mocked_remove_envoyfilter,
        mocked_kubernetes_resource_handler_class,
        harness,
        mocked_lightkube_client,
        mocker,
    ):
        """Tests the upgrade method is called when the CNI config is changed."""
        harness.set_leader(True)
        harness.begin()
        mocked_upgrade_charm = mocker.patch("charm.Operator.upgrade_charm")
        harness.charm.reconcile("event")
        mocked_upgrade_charm.assert_called_once()

    def test_remove_gateway(
        self,
        harness,
        kubernetes_resource_handler_with_client_and_existing_gateway,
        mocked_cert_subject,
    ):
        """Tests that _remove_gateway works when expected.

        Uses the kubernetes_resource_handler_with_client_and_existing_gateway pre-made
        environment which has exactly one existing gateway that will be returned during a
        client.list().
        """
        # Arrange
        (
            mocked_krh_class,
            mocked_lightkube_client,
            existing_gateway_name,
        ) = kubernetes_resource_handler_with_client_and_existing_gateway

        harness.begin()

        # Act
        harness.charm._remove_gateway()

        # Assert
        # We've mocked the list method very broadly.  Ensure we only get called the time we expect
        assert mocked_lightkube_client.list.call_count == 2
        created_resources = [
            args_list.args[0] for args_list in mocked_lightkube_client.list.call_args_list
        ]
        assert GATEWAY_LIGHTKUBE_RESOURCE in created_resources
        assert Secret in created_resources

        # Assert that we tried to remove the old gateway
        assert mocked_lightkube_client.delete.call_args.kwargs["name"] == existing_gateway_name

    @patch("charm.Client", return_value=MagicMock())
    def test_remove_envoyfilter(
        self,
        mocked_lightkube_client_class,
        mocked_cert_subject,
    ):
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
            (404, does_not_raise()),  # 404 errors are ignored
        ],
    )
    @patch("charm.Client", return_value=MagicMock())
    def test_remove_envoyfilter_error_handling(
        self, mocked_lightkube_client_class, error_code, context_raised
    ):
        """Test that _renove_envoyfilter handles errors as expected."""
        name = "test"
        namespace = "test-namespace"
        mocked_lightkube_client = mocked_lightkube_client_class.return_value
        mocked_lightkube_client.delete.side_effect = FakeApiError(error_code)

        with context_raised:
            _remove_envoyfilter(name, namespace)

    @pytest.mark.parametrize(
        "errors, expected_status_type",
        [
            ([], ActiveStatus),
            (
                [
                    ErrorWithStatus("0", BlockedStatus),
                    ErrorWithStatus("1", BlockedStatus),
                    ErrorWithStatus("0", WaitingStatus),
                    ErrorWithStatus("0", MaintenanceStatus),
                ],
                BlockedStatus,
            ),
            ([ErrorWithStatus("0", WaitingStatus)], WaitingStatus),
        ],
    )
    def test_report_handled_errors(
        self,
        errors,
        expected_status_type,
        harness,
        mocked_cert_subject,
    ):
        """Tests that _report_handled_errors notifies users of errors via status and logging."""
        # Arrange
        harness.begin()

        # Mock the logger
        harness.charm.log = MagicMock()

        # Act
        harness.charm._report_handled_errors(errors)

        # Assert
        assert isinstance(harness.model.unit.status, expected_status_type)
        if isinstance(harness.model.unit.status, ActiveStatus):
            assert harness.charm.log.info.call_count == 0
        else:
            assert f"handled {len(errors)} errors" in harness.model.unit.status.message
            assert (
                harness.charm.log.info.call_count
                + harness.charm.log.warning.call_count
                + harness.charm.log.error.call_count
                == len(errors) + 1
            )

    @pytest.mark.parametrize(
        "related_applications, gateway_status",
        [
            ([], True),  # No related applications
            (["other1"], True),  # A single related application
            (["other1", "other2", "other3"], True),  # Multiple related applications
            (["other1"], False),  # Gateway is offline
        ],
    )
    @patch("charm.Operator._is_gateway_up", new_callable=PropertyMock)
    def test_send_gateway_info(
        self,
        mocked_is_gateway_up,
        related_applications,
        gateway_status,
        harness,
        mocked_cert_subject,
    ):
        """Tests that send_gateway_info handler for the gateway-info relation works as expected."""
        # Assert
        # Must be leader because we write to the application part of the relation data
        gateway_name = "test-gateway"
        model_name = "some-model"
        harness.update_config({"default-gateway": gateway_name})
        harness.set_leader(True)
        harness.set_model_name(model_name)

        relation_info = [
            add_gateway_info_to_harness(harness, other_app=name) for name in related_applications
        ]
        harness.begin()

        # Mock the gateway service status
        mocked_is_gateway_up.return_value = gateway_status

        expected_data = {
            "gateway_name": gateway_name,
            "gateway_namespace": model_name,
            "gateway_up": str(gateway_status).lower(),
        }

        # Act
        harness.charm._send_gateway_info()

        # Assert on the relation data
        # The correct number of relations exist
        assert len(harness.model.relations["gateway-info"]) == len(relation_info)

        # For each relation, the relation data is correct
        for this_relation_info in relation_info:
            actual_data = harness.get_relation_data(this_relation_info["rel_id"], "istio-pilot")
            assert expected_data == actual_data

    @pytest.mark.parametrize(
        "cert_handler_enabled, ssl_cert, ssl_key, expected_return, expected_context",
        [
            (False, "", "", False, does_not_raise()),
            (True, "x", "y", True, does_not_raise()),
            (True, "x", "", None, pytest.raises(ErrorWithStatus)),
            (True, "", "y", None, pytest.raises(ErrorWithStatus)),
        ],
    )
    def test_use_https_with_tls_provider(
        self,
        cert_handler_enabled,
        ssl_cert,
        ssl_key,
        expected_return,
        expected_context,
        harness,
        mocked_cert_subject,
    ):
        """Test the method returns the correct boolean when the CertHandler is defined."""
        harness.begin()
        harness.charm._cert_handler = MagicMock()
        harness.charm._cert_handler.enabled = cert_handler_enabled
        harness.charm._cert_handler.cert = ssl_cert
        harness.charm._cert_handler.key = ssl_key

        with expected_context:
            assert harness.charm._use_https_with_tls_provider() == expected_return

    @pytest.mark.parametrize(
        "left, right, expected",
        [
            (True, False, True),
            (False, True, True),
            (True, True, False),
            (False, False, False),
        ],
    )
    def test_xor(self, left, right, expected):
        """Test that the xor helper function works as expected."""
        assert _xor(left, right) is expected

    def test_get_config(
        self,
        harness,
        mocked_cert_subject,
    ):
        """Test configuration retrieval function."""
        harness.begin()
        image_config = harness.charm._get_image_config()
        assert "pilot-image" in image_config.keys()
        assert "pilot" == image_config["pilot-image"]
        assert "proxyv2" == image_config["global-proxy-image"]


class TestCharmUpgrade:
    """Tests for charm upgrade handling."""

    @patch("charm._wait_for_update_rollout")  # Do not wait for upgrade to finish
    @patch("charm._validate_upgrade_version")  # Do not validate versions
    @patch("charm.Istioctl", return_value=MagicMock())
    def test_upgrade_successful(
        self,
        mocked_istioctl_class,
        _mocked_validate_upgrade_version,
        mocked_wait_for_update_rollout,
        harness,
        mocked_cert_subject,
    ):
        """Tests that charm.upgrade_charm works successfully when expected."""
        model_name = "test-model"
        harness.set_model_name(model_name)

        mocked_istioctl = mocked_istioctl_class.return_value

        # Return valid version data from istioctl.versions
        mocked_istioctl.version.return_value = {"client": "1.15.7", "control_plane": "1.15.7"}

        # Simulate the upgrade
        harness.begin()
        harness.charm.upgrade_charm("mock_event")

        # Assert that the upgrade was successful
        mocked_istioctl_class.assert_called_with(
            "./istioctl",
            model_name,
            "minimal",
            istioctl_extra_flags=[
                "--set",
                "values.pilot.image=pilot",
                "--set",
                "values.global.tag=1.17.3",
                "--set",
                "values.global.hub=docker.io/istio",
                "--set",
                "values.global.proxy.image=proxyv2",
                "--set",
                "values.global.proxy_init.image=proxyv2",
            ],
        )
        mocked_istioctl.upgrade.assert_called_with()

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
        mocked_cert_subject,
    ):
        """Tests that charm.upgrade_charm fails when precheck fails."""
        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @patch("charm.Istioctl.version", side_effect=IstioctlError())
    def test_upgrade_failed_getting_version(
        self,
        _mocked_istioctl_version,
        harness,
        mocked_cert_subject,
    ):
        """Tests that charm.upgrade_charm fails when getting version fails."""
        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @patch("charm._validate_upgrade_version", side_effect=ValueError())  # Fail when validating
    @patch("charm.Istioctl.version")  # Pass istioctl version check
    def test_upgrade_failed_version_check(
        self,
        _mocked_istioctl_version,
        _mocked_validate_upgrade_version,
        harness,
        mocked_cert_subject,
    ):
        """Tests that charm.upgrade_charm fails when version check fails."""
        model_name = "test-model"
        harness.set_model_name(model_name)

        harness.begin()

        with pytest.raises(GenericCharmRuntimeError):
            harness.charm.upgrade_charm("mock_event")

    @patch("charm._validate_upgrade_version")  # Do not validate versions
    @patch("charm.Istioctl.version")  # Pass istioctl version check
    @patch("charm.Istioctl.precheck")  # Fail istioctl precheck
    @patch("charm.Istioctl.upgrade", side_effect=IstioctlError())  # Fail istioctl upgrade
    def test_upgrade_failed_during_upgrade(
        self,
        _mocked_istioctl_upgrade,
        _mocked_istioctl_precheck,
        _mocked_istioctl_version,
        _mocked_validate_upgrade_version,
        harness,
        mocked_cert_subject,
    ):
        """Tests that charm.upgrade_charm fails during upgrade."""
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

    # ---- Start of block
    # ---- Test cases added for testing the TLS secret feature, remove after 1.21
    @pytest.mark.parametrize(
        "https_with_tls_provider, https_with_tls_secret, expected_return, expected_context",
        [
            (True, False, True, does_not_raise()),
            (False, True, True, does_not_raise()),
            (False, False, False, does_not_raise()),
            (True, True, None, pytest.raises(ErrorWithStatus)),
        ],
    )
    def test_use_https(
        self,
        https_with_tls_provider,
        https_with_tls_secret,
        expected_return,
        expected_context,
        harness,
        mocked_cert_subject,
    ):
        """Test the method returns a correct bool when comparing two TLS options.

        Parameters:
         https_with_tls_provider(bool): the return of the method that checks
             the TLS certificates provider is related and configured to this charm
         https_with_tls_secret(bool): the return of the method that checks the TLS
             configuration is done from secret data
         expected_return(bool, None): the expected return of the methods above
         expected_context: the exception(if any) that is expected when calling
             the above methods.

         Example:
          If https_with_tls_provider returns False and https_with_tls_secret returns True,
          the expected return of _use_https() is True.
          If both https_with_tls_provider and https_with_tls_secret return True, _use_https()
          must raise ErrorWithStatus.
        """
        harness.begin()

        harness.charm._use_https_with_tls_secret = MagicMock()
        harness.charm._use_https_with_tls_secret.return_value = https_with_tls_secret
        harness.charm._use_https_with_tls_provider = MagicMock()
        harness.charm._use_https_with_tls_provider.return_value = https_with_tls_provider

        with expected_context:
            assert harness.charm._use_https() == expected_return

    @pytest.mark.skip("Skipping due to ValueError: Secret owner cannot use refresh=True")
    @pytest.mark.parametrize(
        "ssl_cert, ssl_key, expected_return, expected_context",
        [
            ("", "", None, pytest.raises(GenericCharmRuntimeError)),
            ("x", "y", True, does_not_raise()),
            ("x", "", None, pytest.raises(GenericCharmRuntimeError)),
            ("", "y", None, pytest.raises(GenericCharmRuntimeError)),
        ],
    )
    def test_use_https_with_tls_secret_found(
        self,
        ssl_cert,
        ssl_key,
        expected_return,
        expected_context,
        harness,
        mocked_cert_subject,
    ):
        """Test the method returns a correct bool when the secret is added.

        Parameters:
          ssl_cert(str): SSL cert value (comes from a secret)
          ssl_key(str): SSL key value (comes from a secret)
          expected_return(bool): the expected return of the use_https_with_tls_secret method
          expected_context: if the method should raise or not
        Example:
          If ssl_cert and ssl_key both have values, the use_https_with_tls_secret() method
          must return True; if both are empty the return must be False; and if just one
          value is set, the method should raise ErrorWithStatus.
        """
        harness.begin()
        harness.charm.app.add_secret(
            content={"ssl-key": ssl_key, "ssl-crt": ssl_cert}, label=TLS_SECRET_LABEL
        )

        with expected_context:
            assert harness.charm._use_https_with_tls_secret() == expected_return

    def test_use_https_with_tls_secret_not_found(self, harness, mocked_cert_subject):
        """Test the method returns False when the secret is not added."""
        harness.begin()
        assert harness.charm._use_https_with_tls_secret() is False

    def test_set_tls_set_secret_content(
        self,
        harness,
        mocked_cert_subject,
        all_operator_reconcile_handlers_mocked,
    ):
        """Test the method sets secret content when secret exists."""
        harness.begin()
        harness.add_relation("peers", harness.charm.app.name)
        harness.set_leader(True)

        mocked_action_event = MagicMock(spec=ActionEvent)
        mocked_action_event.params = {"ssl-key": "new-key", "ssl-crt": "new-crt"}

        # Add secret first so the charm code detects it and just updates the value
        harness.charm.app.add_secret(
            content={"ssl-key": "current-key", "ssl-crt": "current-crt"}, label=TLS_SECRET_LABEL
        )
        harness.charm.set_tls(mocked_action_event)
        assert (
            harness.model.get_secret(label=TLS_SECRET_LABEL).get_content()
            == mocked_action_event.params
        )

    def test_set_tls_add_secret(
        self, harness, mocked_cert_subject, all_operator_reconcile_handlers_mocked
    ):
        """Test the method adds a secret when it does not exist."""
        harness.begin()
        harness.add_relation("peers", harness.charm.app.name)
        harness.set_leader(True)

        mocked_action_event = MagicMock(spec=ActionEvent)
        mocked_action_event.params = {"ssl-key": "new-key", "ssl-crt": "new-crt"}

        harness.charm.set_tls(mocked_action_event)
        assert (
            harness.model.get_secret(label=TLS_SECRET_LABEL).get_content()
            == mocked_action_event.params
        )

    def test_unset_tls(self, harness, all_operator_reconcile_handlers_mocked, mocked_cert_subject):
        """Test the secret gets removed."""
        harness.begin()
        harness.add_relation("peers", harness.charm.app.name)
        harness.set_leader(True)

        mocked_action_event = MagicMock(spec=ActionEvent)

        # Add secret first so the charm code detects it and is able to delete it
        harness.charm.app.add_secret(
            content={"ssl-key": "key", "ssl-crt": "crt"}, label=TLS_SECRET_LABEL
        )
        harness.charm.unset_tls(mocked_action_event)

        with pytest.raises(SecretNotFoundError):
            harness.model.get_secret(label=TLS_SECRET_LABEL)

    # ---- End of block


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
            "spec": {"type": "ClusterIP", "clusterIP": "10.10.10.11"},
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
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.12"},
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
            "spec": {"type": "LoadBalancer", "clusterIP": "10.10.10.12"},
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


@pytest.fixture()
def mocked_lightkube_client(mocked_lightkube_client_class):
    mocked_instance = MagicMock()
    mocked_lightkube_client_class.return_value = mocked_instance
    yield mocked_instance


@pytest.fixture()
def mocked_lightkube_client_class(mocker):
    mocked = mocker.patch("charm.Client")
    yield mocked


@pytest.fixture()
def mocked_cert_subject(mocker):
    mocked_cert_subject = mocker.patch("charm.Operator._cert_subject")
    mocked_cert_subject.return_value = "gateway-address"
    return mocked_cert_subject


# Helpers
def add_data_to_sdi_relation(
    harness: Harness,
    rel_id: str,
    other: str,
    data: Optional[dict] = None,
    supported_versions: str = "- v1",
) -> None:
    """Add data to the an SDI-backed relation."""
    if data is None:
        data = {}

    harness.update_relation_data(
        rel_id,
        other,
        {"_supported_versions": supported_versions, "data": yaml.dump(data)},
    )


def add_gateway_info_to_harness(harness: Harness, other_app="other") -> dict:
    """Relates a new app and unit to the gateway-info relation.

    Returns dict of:
    * other (str): The name of the other app
    * other_unit (str): The name of the other unit
    * rel_id (int): The relation id
    * data (dict): The relation data put to the relation
    """
    other_unit = f"{other_app}/0"
    rel_id = harness.add_relation("gateway-info", other_app)

    harness.add_relation_unit(rel_id, other_unit)
    data = {}

    return {
        "other_app": other_app,
        "other_unit": other_unit,
        "rel_id": rel_id,
        "data": data,
    }


def add_ingress_auth_to_harness(harness: Harness, other_app="other") -> dict:
    """Relates a new app and unit to the ingress-auth relation.

    Returns dict of:
    * other (str): The name of the other app
    * other_unit (str): The name of the other unit
    * rel_id (int): The relation id
    * data (dict): The relation data put to the relation
    """
    other_unit = f"{other_app}/0"
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
        "other_app": other_app,
        "other_unit": other_unit,
        "rel_id": rel_id,
        "data": data,
    }


def add_ingress_to_harness(harness: Harness, other_app="other") -> dict:
    """Relates a new app and unit to the ingress relation.

    Returns dict of:
    * other (str): The name of the other app
    * other_unit (str): The name of the other unit
    * rel_id (int): The relation id
    * data (dict): The relation data put to the relation
    """
    other_unit = f"{other_app}/0"
    rel_id = harness.add_relation("ingress", other_app)

    harness.add_relation_unit(rel_id, other_unit)
    data = {
        "service": f"{other_app}",
        "port": 8888,
        "namespace": f"{other_app}-namespace",
        "prefix": f"{other_app}-prefix",
        "rewrite": f"{other_app}-rewrite",
    }
    add_data_to_sdi_relation(harness, rel_id, other_app, data, supported_versions="- v2")

    return {
        "other_app": other_app,
        "other_unit": other_unit,
        "rel_id": rel_id,
        "data": data,
    }


def add_istio_pilot_to_harness(harness: Harness, other_app="other") -> dict:
    """Relates a new app and unit to the istio-pilot relation.

    Returns dict of:
    * other (str): The name of the other app
    * other_unit (str): The name of the other unit
    * rel_id (int): The relation id
    * data (dict): The relation data put to the relation
    """
    other_unit = f"{other_app}/0"
    rel_id = harness.add_relation("istio-pilot", other_app)

    harness.add_relation_unit(rel_id, other_unit)
    data = {}
    add_data_to_sdi_relation(harness, rel_id, other_app, data, supported_versions="- v1")

    return {
        "other_app": other_app,
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


def assert_envoyfilter_applied_to_all_gateway_ports(
    krh_lightkube_client, envoyfilter_name, model_name
):
    # Assert that the EnvoyFilter is applied to all relevant ports
    envoyfilter_call_arg = get_lightkube_resource_in_call_args_list(
        krh_lightkube_client.apply.call_args_list, envoyfilter_name, model_name
    )
    config_patches = envoyfilter_call_arg.kwargs["obj"]["spec"]["configPatches"]
    patched_ports = set()
    for config_patch in config_patches:
        patched_ports.add(int(config_patch["match"]["listener"]["portNumber"]))
    assert patched_ports.issubset(GATEWAY_PORTS.values())
    krh_lightkube_client.reset_mock()


def is_lightkube_resource_in_call_args_list(call_args_list, name, namespace=None):
    """Returns a boolean of whether a call in the list includes this lightkube resource.

    Args:
        call_args_list (list): The list of call args to search, as given by a mock.call_args_list
        name (str): The name of the lightkube resource to search for (will check metadata.name)
        namespace (str): The namespace of the lightkube resource to search for (will check
                         metadata.namespace)
    """
    try:
        get_lightkube_resource_in_call_args_list(
            call_args_list=call_args_list, name=name, namespace=namespace
        )
        return True
    except KeyError:
        return False


def get_lightkube_resource_in_call_args_list(call_args_list, name, namespace=None):
    """Returns the first call_args from a call_args_list that was for a given lightkube resource.

    Raises a KeyError if the object does not exist.

    Args:
        call_args_list (list): The list of call args to search, as given by a mock.call_args_list
        name (str): The name of the lightkube resource to search for (will check metadata.name)
        namespace (str): The namespace of the lightkube resource to search for (will check
                         metadata.namespace)

    Return: The lightkube resource
    """
    for call_args in call_args_list:
        try:
            if (
                call_args.kwargs["obj"].metadata.name == name
                and getattr(call_args.kwargs["obj"].metadata, "namespace", None) == namespace
            ):
                return call_args
        except Exception:
            continue
    raise KeyError(f"Resource {name} in namespace {namespace} not found in this call_args list.")
