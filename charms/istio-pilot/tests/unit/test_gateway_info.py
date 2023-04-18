#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from charms.istio_pilot.v0.istio_gateway_info import (
    GatewayRequirer,
)
from ops.charm import CharmBase
from ops.testing import Harness

REQUIRER_CHARM_META = f"""
name: test-charm
requires:
  test-relation:
    interface: test-interface
"""

class GenericCharm(CharmBase):
    pass

@pytest.fixture()
def requirer_charm_harness():
    return Harness(GenericCharm, meta=REQUIRER_CHARM_META)

def test_get_relation_data_passes(requirer_charm_harness, mocker):
    """Assert the relation data is as expected."""
    # Initial configuration
    requirer_charm_harness.set_model_name("test-model")
    requirer_charm_harness.begin()
    requirer_charm_harness.set_leader(True)
    relation_id = requirer_charm_harness.add_relation("test-relation", "app")

    # Instantiate GatewayRequirer class
    requirer_charm_harness.charm.gateway_requirer = GatewayRequirer(requirer_charm_harness.charm, relation_name="test-relation")

    # Add and update relation 
    expected_relation_data = {"gateway_name": "name", "gateway_namespace": "namespace"}
    requirer_charm_harness.add_relation_unit(relation_id, "app/0")
    requirer_charm_harness.update_relation_data(relation_id, "app", expected_relation_data)

    # Get the relation data
    actual_relation_data = requirer_charm_harness.charm.gateway_requirer.get_relation_data()

    # Assert returns dictionary with expected values
    assert actual_relation_data == expected_relation_data
