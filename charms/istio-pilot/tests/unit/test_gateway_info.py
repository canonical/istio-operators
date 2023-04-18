#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from charms.istio_pilot.v0.istio_gateway_info import (
    GatewayRequirer,
)
from ops.charm import CharmBase
from ops.testing import Harness

DEFAULT_RELATION_NAME = "gateway-info"
DEFAULT_INTERFACE_NAME = "istio-gateway-info"
TEST_CHARM_META = f"""
name: test-charm
requires:
  {DEFAULT_RELATION_NAME}:
    interface: {DEFAULT_INTERFACE_NAME}
"""

class GenericCharm(CharmBase):
    pass

@pytest.fixture()
def generic_charm_harness():
    return Harness(GenericCharm, meta=TEST_CHARM_META)

def test_get_relation_data_passes(generic_charm_harness, mocker):
    """Assert the relation data is as expected."""
    # Initial configuration
    generic_charm_harness.set_model_name("test-model")
    generic_charm_harness.begin()
    generic_charm_harness.set_leader(True)
    relation_id = generic_charm_harness.add_relation(DEFAULT_RELATION_NAME, "app")

    # Instantiate GatewayRequirer class
    generic_charm_harness.charm.gateway_requirer = GatewayRequirer(generic_charm_harness.charm)

    # Add and update relation 
    expected_relation_data = {"gateway_name": "name", "gateway_namespace": "namespace"}
    generic_charm_harness.add_relation_unit(relation_id, "app/0")
    generic_charm_harness.update_relation_data(relation_id, "app", expected_relation_data)

    # Get the relation data
    actual_relation_data = generic_charm_harness.charm.gateway_requirer.get_relation_data()

    # Assert returns dictionary with expected values
    assert actual_relation_data == expected_relation_data
