# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest
import yaml
from charmed_kubeflow_chisme.testing import (
    APP_METRICS_ENDPOINT,
    GRAFANA_AGENT_APP,
    GRAFANA_AGENT_METRICS_ENDPOINT,
    assert_alert_rules,
    assert_grafana_dashboards,
    assert_metrics_endpoint,
    deploy_and_assert_grafana_agent,
    get_alert_rules,
    get_grafana_dashboards,
)
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

ISTIO_GATEWAY_METADATA = yaml.safe_load(Path("charms/istio-gateway/metadata.yaml").read_text())
ISTIO_PILOT_METADATA = yaml.safe_load(Path("charms/istio-pilot/metadata.yaml").read_text())
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
ISTIO_PILOT_APP_NAME = "istio-pilot"
ISTIO_PILOT_ALER_RULES = Path("./charms/istio-pilot/src/prometheus_alert_rules")
ISTIO_PILOT_DASHBOARDS = Path("./charms/istio-pilot/src/grafana_dashboards")
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
ISTIO_GATEWAY_ALER_RULES = Path("./charms/istio-gateway/src/prometheus_alert_rules")


@pytest.mark.abort_on_fail
async def test_build_and_deploy_istio_charms(ops_test: OpsTest, request):
    # Build, deploy, and relate istio charms
    istio_gateway_name = ISTIO_GATEWAY_METADATA["name"]
    istio_pilot_name = ISTIO_PILOT_METADATA["name"]
    if charms_path := request.config.getoption("--charms-path"):
        istio_gateway = (
            f"{charms_path}/{istio_gateway_name}/{istio_gateway_name}_ubuntu@24.04-amd64.charm"
        )
        istio_pilot = (
            f"{charms_path}/{istio_pilot_name}/{istio_pilot_name}_ubuntu@24.04-amd64.charm"
        )
    else:
        istio_gateway = await ops_test.build_charm("charms/istio-gateway")
        istio_pilot = await ops_test.build_charm("charms/istio-pilot")

    await ops_test.model.deploy(
        istio_pilot,
        application_name=ISTIO_PILOT_APP_NAME,
        trust=True,
    )

    await ops_test.model.deploy(
        istio_gateway,
        application_name=ISTIO_GATEWAY_APP_NAME,
        config={"kind": "ingress"},
        trust=True,
    )

    await ops_test.model.integrate(
        f"{ISTIO_PILOT_APP_NAME}:istio-pilot", f"{ISTIO_GATEWAY_APP_NAME}:istio-pilot"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )
    # Deploying grafana-agent-k8s and add all relations
    await deploy_and_assert_grafana_agent(
        ops_test.model, ISTIO_PILOT_APP_NAME, metrics=True, dashboard=True, logging=False
    )
    # Note(rgildein): Using this until the [1] is not fixed.
    # [1]: https://github.com/canonical/charmed-kubeflow-chisme/issues/117
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
    [
        (ISTIO_PILOT_APP_NAME, "/metrics", 15014),
        (ISTIO_GATEWAY_APP_NAME, "/stats/prometheus", 9090),
    ],
)
async def test_metrics_enpoint(charm, metrics_path, metrics_port, ops_test):
    """Test metrics_endpoints are defined in relation data bag and their accessibility.
    This function gets all the metrics_endpoints from the relation data bag, checks if
    they are available from the grafana-agent-k8s charm and finally compares them with the
    ones provided to the function.
    """
    app = ops_test.model.applications[charm]
    await assert_metrics_endpoint(app, metrics_port=metrics_port, metrics_path=metrics_path)


@pytest.mark.parametrize(
    "charm, path_to_alert_rules",
    [
        (ISTIO_PILOT_APP_NAME, ISTIO_PILOT_ALER_RULES),
        (ISTIO_GATEWAY_APP_NAME, ISTIO_GATEWAY_ALER_RULES),
    ],
)
async def test_alert_rules(charm, path_to_alert_rules, ops_test):
    """Test check charm alert rules and rules defined in relation data bag."""
    app = ops_test.model.applications[charm]
    alert_rules = get_alert_rules(path_to_alert_rules)
    log.info("found alert_rules: %s", alert_rules)
    await assert_alert_rules(app, alert_rules)


@pytest.mark.parametrize(
    "charm, path_to_dashboards", [(ISTIO_PILOT_APP_NAME, ISTIO_PILOT_DASHBOARDS)]
)
async def test_grafana_dashboards(charm, path_to_dashboards, ops_test):
    """Test Grafana dashboards are defined in relation data bag."""
    app = ops_test.model.applications[charm]
    dashboards = get_grafana_dashboards(path_to_dashboards)
    log.info("found dashboards: %s", dashboards)
    await assert_grafana_dashboards(app, dashboards)
