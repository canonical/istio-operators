# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
import json
import logging
from pathlib import Path

import pytest
import requests
import tenacity
import yaml
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

ISTIO_PILOT = "istio-pilot"
ISTIO_INGRESSGATEWAY = "istio-ingressgateway"


@pytest.mark.abort_on_fail
async def test_deploy_istio_charms(ops_test: OpsTest):
    # Build, deploy, and relate istio charms
    charms_path = "./charms/istio"
    istio_charms = await ops_test.build_charms(f"{charms_path}-gateway", f"{charms_path}-pilot")

    await ops_test.model.deploy(
        istio_charms["istio-pilot"], application_name=ISTIO_PILOT, trust=True
    )
    await ops_test.model.deploy(
        istio_charms["istio-gateway"],
        application_name=ISTIO_INGRESSGATEWAY,
        config={"kind": "ingress"},
        trust=True,
    )

    await ops_test.model.add_relation(
        f"{ISTIO_PILOT}:istio-pilot", f"{ISTIO_INGRESSGATEWAY}:istio-pilot"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )


async def test_prometheus_grafana_integration_istio_pilot(ops_test: OpsTest):
    """Deploy prometheus and required relations, then test the metrics."""
    prometheus = "prometheus-k8s"
    prometheus_scrape = "prometheus-scrape-config-k8s"
    scrape_config = {"scrape_interval": "30s"}

    # Deploy and relate prometheus
    await ops_test.model.deploy(prometheus, channel="latest/stable", trust=True)
    await ops_test.model.deploy(prometheus_scrape, channel="latest/stable", config=scrape_config)

    await ops_test.model.add_relation(ISTIO_PILOT, prometheus_scrape)
    await ops_test.model.add_relation(
        f"{prometheus}:metrics-endpoint", f"{prometheus_scrape}:metrics-endpoint"
    )

    await ops_test.model.wait_for_idle(status="active", timeout=90 * 10)

    status = await ops_test.model.get_status()
    prometheus_unit_ip = status["applications"][prometheus]["units"][f"{prometheus}/0"]["address"]
    log.info(f"Prometheus available at http://{prometheus_unit_ip}:9090")

    for attempt in retry_for_5_attempts:
        log.info(
            f"Testing prometheus deployment (attempt " f"{attempt.retry_state.attempt_number})"
        )
        with attempt:
            r = requests.get(
                f"http://{prometheus_unit_ip}:9090/api/v1/query?"
                f'query=up{{juju_application="{ISTIO_PILOT}"}}'
            )
            response = json.loads(r.content.decode("utf-8"))
            response_status = response["status"]
            log.info(f"Response status is {response_status}")
            assert response_status == "success"

            response_metric = response["data"]["result"][0]["metric"]
            assert response_metric["juju_application"] == ISTIO_PILOT
            assert response_metric["juju_model"] == ops_test.model_name


async def test_istio_pilot_alert_rules(ops_test: OpsTest):
    """Test alert rules availability and match with what is found in the source code."""

    prometheus = "prometheus-k8s"
    status = await ops_test.model.get_status()
    prometheus_unit_ip = status["applications"][prometheus]["units"][f"{prometheus}/0"]["address"]

    # Get targets and assert they are available
    targets_url = f"http://{prometheus_unit_ip}:9090/api/v1/targets"
    for attempt in retry_for_5_attempts:
        log.info(
            f"Reaching Prometheus targets... (attempt " f"{attempt.retry_state.attempt_number})"
        )
        with attempt:
            r = requests.get(targets_url)
            targets_result = json.loads(r.content.decode("utf-8"))
    assert targets_result is not None
    assert targets_result["status"] == "success"

    # Verify that istio-pilot is in the target list
    discovered_labels = targets_result["data"]["activeTargets"][0]["discoveredLabels"]
    assert discovered_labels["juju_application"] == "istio-pilot"

    # Get available alert rules from Prometheus and assert they are available
    rules_url = f"http://{prometheus_unit_ip}:9090/api/v1/rules"
    for attempt in retry_for_5_attempts:
        log.info(
            f"Reaching Prometheus alert rules... (attempt "
            f"{attempt.retry_state.attempt_number})"
        )
        with attempt:
            r = requests.get(rules_url)
            alert_rules_result = json.loads(r.content.decode("utf-8"))

    assert alert_rules_result is not None
    assert alert_rules_result["status"] == "success"
    actual_rules = []
    for group in alert_rules_result["data"]["groups"]:
        actual_rules.append(group["rules"][0])

    # Verify expected alerts vs actual alerts in Prometheus
    istio_pilot_alert_rules = glob.glob("charms/istio-pilot/src/prometheus_alert_rules/*.rule")
    expected_rules = []
    for alert_rule in istio_pilot_alert_rules:
        alert_object = yaml.safe_load(Path(alert_rule).read_text())
        expected_rules.append(alert_object["alert"])
    assert len(expected_rules) == len(actual_rules)

    # Verify istio_pilot alert rules match the actual alert rules
    for rule in actual_rules:
        assert rule["name"] in expected_rules


# Helper to retry calling a function over 30 seconds or 5 attempts
retry_for_5_attempts = tenacity.Retrying(
    stop=(tenacity.stop_after_attempt(5) | tenacity.stop_after_delay(30)),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
