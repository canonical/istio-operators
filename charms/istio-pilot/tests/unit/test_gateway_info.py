#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from charms.istio_pilot.v0.istio_gateway_info import (
    GatewayProvider,
    GatewayRelationDataMissingError,
    GatewayRelationMissingError,
    GatewayRequirer,
)
from ops.charm import CharmBase
from ops.model import TooManyRelatedAppsError
from ops.testing import Harness

DEFAULT_RELATION_NAME = "gateway-info"
DEFAULT_INTERFACE_NAME = "istio-gateway-info"
TEST_RELATION_NAME = "test-relation"
REQUIRER_CHARM_META = f"""
name: requirer-test-charm
requires:
  {TEST_RELATION_NAME}:
    interface: test-interface
"""
PROVIDER_CHARM_META = f"""
name: provider-test-charm
provides:
  {TEST_RELATION_NAME}:
    interface: test-interface
"""


class GenericCharm(CharmBase):
    pass


@pytest.fixture()
def requirer_charm_harness():
    return Harness(GenericCharm, meta=REQUIRER_CHARM_META)


@pytest.fixture()
def provider_charm_harness():
    return Harness(GenericCharm, meta=PROVIDER_CHARM_META)


def test_get_relation_data_passes(requirer_charm_harness):
    """Assert the relation data is as expected."""
    # Initial configuration
    requirer_charm_harness.set_model_name("test-model")
    requirer_charm_harness.begin()
    requirer_charm_harness.set_leader(True)
    relation_id = requirer_charm_harness.add_relation(TEST_RELATION_NAME, "app")

    # Instantiate GatewayRequirer class
    requirer_charm_harness.charm.gateway_requirer = GatewayRequirer(
        requirer_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    # Add and update relation
    expected_relation_data = {"gateway_name": "name", "gateway_namespace": "namespace"}
    requirer_charm_harness.add_relation_unit(relation_id, "app/0")
    requirer_charm_harness.update_relation_data(relation_id, "app", expected_relation_data)

    # Get the relation data
    actual_relation_data = requirer_charm_harness.charm.gateway_requirer.get_relation_data()

    # Assert returns dictionary with expected values
    assert actual_relation_data == expected_relation_data


def test_check_raise_too_many_relations(requirer_charm_harness):
    """Assert that TooManyRelatedAppsError is raised if more than one application is related."""
    requirer_charm_harness.set_model_name("test-model")
    requirer_charm_harness.begin()
    requirer_charm_harness.set_leader(True)

    # Instantiate GatewayRequirer class
    requirer_charm_harness.charm.gateway_requirer = GatewayRequirer(
        requirer_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    requirer_charm_harness.add_relation(TEST_RELATION_NAME, "app")
    requirer_charm_harness.add_relation(TEST_RELATION_NAME, "app2")

    with pytest.raises(TooManyRelatedAppsError):
        requirer_charm_harness.charm.gateway_requirer.get_relation_data()


def test_preflight_checks_raise_no_relation(requirer_charm_harness):
    """Assert that GatewayRelationMissingError is raised in the absence of the relation."""
    requirer_charm_harness.set_model_name("test-model")
    requirer_charm_harness.begin()
    requirer_charm_harness.set_leader(True)

    # Instantiate GatewayRequirer class
    requirer_charm_harness.charm.gateway_requirer = GatewayRequirer(
        requirer_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    with pytest.raises(GatewayRelationMissingError):
        requirer_charm_harness.charm.gateway_requirer.get_relation_data()


def test_preflight_checks_raise_no_relation_data(requirer_charm_harness):
    """Assert that GatewayRelationDataMissingError is raised in the absence of relation data."""
    requirer_charm_harness.set_model_name("test-model")
    requirer_charm_harness.begin()
    requirer_charm_harness.set_leader(True)

    # Instantiate GatewayRequirer class
    requirer_charm_harness.charm.gateway_requirer = GatewayRequirer(
        requirer_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    requirer_charm_harness.add_relation(TEST_RELATION_NAME, "app")

    with pytest.raises(GatewayRelationDataMissingError) as error:
        requirer_charm_harness.charm.gateway_requirer.get_relation_data()
    assert str(error.value) == "No data found in relation data bag."


def test_preflight_checks_raises_data_missing_attribute(requirer_charm_harness):
    """Assert that GatewayRelationDataMissingError is raised when
    the relation data bag missing an expected attribute.
    """
    # Initial configuration
    requirer_charm_harness.set_model_name("test-model")
    requirer_charm_harness.begin()
    requirer_charm_harness.set_leader(True)
    relation_id = requirer_charm_harness.add_relation(TEST_RELATION_NAME, "app")

    # Instantiate GatewayRequirer class
    requirer_charm_harness.charm.gateway_requirer = GatewayRequirer(
        requirer_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    # Add and update relation
    faulty_relation_data = {"gateway_name": "name"}
    requirer_charm_harness.add_relation_unit(relation_id, "app/0")
    requirer_charm_harness.update_relation_data(relation_id, "app", faulty_relation_data)

    with pytest.raises(GatewayRelationDataMissingError) as error:
        requirer_charm_harness.charm.gateway_requirer.get_relation_data()
    assert str(error.value) == "Missing attributes: ['gateway_namespace']"


def test_send_relation_data_passes(provider_charm_harness):
    """Assert the relation data is as expected."""
    # Initial configuration
    provider_charm_harness.set_model_name("test-model")
    provider_charm_harness.begin()
    provider_charm_harness.set_leader(True)
    relation_id = provider_charm_harness.add_relation(TEST_RELATION_NAME, "app")

    # Instantiate GatewayProvider class
    provider_charm_harness.charm.gateway_provider = GatewayProvider(
        provider_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    # Add and update relation
    expected_relation_data = {"gateway_name": "test-gateway", "gateway_namespace": "test"}
    provider_charm_harness.add_relation_unit(relation_id, "app/0")

    provider_charm_harness.charm.gateway_provider.send_gateway_relation_data(
        gateway_name="test-gateway", gateway_namespace="test"
    )
    relation = provider_charm_harness.model.get_relation(TEST_RELATION_NAME)
    actual_relation_data = relation.data[provider_charm_harness.charm.app]
    # Assert returns dictionary with expected values
    assert actual_relation_data == expected_relation_data


def test_provider_raise_no_relation(provider_charm_harness):
    """Assert that GatewayRelationMissingError is raised in the absence of the relation."""
    provider_charm_harness.set_model_name("test-model")
    provider_charm_harness.begin()
    provider_charm_harness.set_leader(True)

    # Instantiate GatewayProvider class
    provider_charm_harness.charm.gateway_provider = GatewayProvider(
        provider_charm_harness.charm, relation_name=TEST_RELATION_NAME
    )

    with pytest.raises(GatewayRelationMissingError):
        provider_charm_harness.charm.gateway_provider.send_gateway_relation_data(
            gateway_name="test-gateway", gateway_namespace="test"
        )
