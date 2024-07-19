# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest
from charmed_kubeflow_chisme.testing import (
    APP_GRAFANA_DASHBOARD,
    APP_METRICS_ENDPOINT,
    GRAFANA_AGENT_APP,
    GRAFANA_AGENT_GRAFANA_DASHBOARD,
    GRAFANA_AGENT_METRICS_ENDPOINT,
    assert_alert_rules,
    assert_metrics_endpoint,
    deploy_and_assert_grafana_agent,
    get_alert_rules,
)
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

ISTIO_PILOT = "istio-pilot"
ISTIO_PILOT_ALER_RULES = Path("./charms/istio-pilot/src/prometheus_alert_rules")
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
ISTIO_GATEWAY_ALER_RULES = Path("./charms/istio-ingressgateway/src/prometheus_alert_rules")


@pytest.mark.abort_on_fail
async def test_build_and_deploy_istio_charms(ops_test: OpsTest):
    # Build, deploy, and relate istio charms
    charms_path = "./charms/istio"
    istio_charms = await ops_test.build_charms(f"{charms_path}-gateway", f"{charms_path}-pilot")

    await ops_test.model.deploy(
        istio_charms["istio-pilot"], application_name=ISTIO_PILOT, series="focal", trust=True
    )
    await ops_test.model.deploy(
        istio_charms["istio-gateway"],
        application_name=ISTIO_GATEWAY_APP_NAME,
        series="focal",
        config={"kind": "ingress"},
        trust=True,
    )

    await ops_test.model.integrate(
        f"{ISTIO_PILOT}:istio-pilot", f"{ISTIO_GATEWAY_APP_NAME}:istio-pilot"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )
    # Deploying grafana-agent-k8s and add all relations
    await deploy_and_assert_grafana_agent(
        ops_test.model, ISTIO_PILOT, metrics=True, dashboard=True, logging=False
    )
    # Note(rgildein): Using this until the [1] is not fixed.
    # [1]: https://github.com/canonical/charmed-kubeflow-chisme/issues/117
    log.info(
        "Adding relation: %s:%s and %s:%s",
        ISTIO_GATEWAY_APP_NAME,
        APP_GRAFANA_DASHBOARD,
        GRAFANA_AGENT_APP,
        GRAFANA_AGENT_GRAFANA_DASHBOARD,
    )
    await ops_test.model.integrate(
        f"{ISTIO_GATEWAY_APP_NAME}:{APP_GRAFANA_DASHBOARD}",
        f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_GRAFANA_DASHBOARD}",
    )
    log.info(
        "Adding relation: %s:%s and %s:%s",
        ISTIO_GATEWAY_APP_NAME,
        APP_METRICS_ENDPOINT,
        GRAFANA_AGENT_APP,
        GRAFANA_AGENT_METRICS_ENDPOINT,
    )
    await ops_test.model.integrate(
        f"{ISTIO_GATEWAY_APP_NAME}:{APP_METRICS_ENDPOINT}",
        f"{GRAFANA_AGENT_APP}:{GRAFANA_AGENT_METRICS_ENDPOINT}",
    )
    await ops_test.model.wait_for_idle(
        apps=[GRAFANA_AGENT_APP], status="blocked", timeout=5 * 60, idle_period=60
    )


@pytest.mark.parametrize(
    "charm, metrics_path, metrics_port",
    [(ISTIO_PILOT, "/metrics", 15014), (ISTIO_GATEWAY_APP_NAME, "/stats/prometheus", 9090)],
)
async def test_metrics_enpoint(charm, metrics_path, metrics_port, ops_test):
    """Test metrics_endpoints are defined in relation data bag and their accessibility.
    This function gets all the metrics_endpoints from the relation data bag, checks if
    they are available from the grafana-agent-k8s charm and finally compares them with the
    ones provided to the function.
    """
    app = ops_test.model.applications[charm]
    await assert_metrics_endpoint(app, metrics_port=metrics_port, metrics_path=metrics_path)


@pytest.mark.parametrize("charm, path_to_alert_rules", [
    (ISTIO_PILOT, ISTIO_PILOT_ALER_RULES), (ISTIO_GATEWAY_APP_NAME, ISTIO_PILOT_ALER_RULES)
])
async def test_alert_rules(charm, path_to_alert_rules, ops_test):
    """Test check charm alert rules and rules defined in relation data bag."""
    app = ops_test.model.applications[charm]
    alert_rules = get_alert_rules(path_to_alert_rules)
    log.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)
