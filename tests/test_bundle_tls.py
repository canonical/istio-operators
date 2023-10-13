import json
import logging
from pathlib import Path
from time import sleep

import aiohttp
import lightkube
import pytest
import requests
import tenacity
import yaml
from bs4 import BeautifulSoup
from lightkube import codecs
from lightkube.generic_resource import (
    create_namespaced_resource,
    load_in_cluster_generic_resources,
)
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

DEX_AUTH = "dex-auth"
OIDC_GATEKEEPER = "oidc-gatekeeper"
ISTIO_PILOT = "istio-pilot"
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
TENSORBOARD_CONTROLLER = "tensorboard-controller"
KUBEFLOW_VOLUMES = "kubeflow-volumes"

USERNAME = "user123"
PASSWORD = "user123"

VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE = create_namespaced_resource(
    group="networking.istio.io",
    version="v1alpha3",
    kind="VirtualService",
    plural="virtualservices",
)


@pytest.mark.abort_on_fail
async def test_kubectl_access(ops_test: OpsTest):
    """Fails if kubectl not available or if no cluster context exists"""
    _, stdout, _ = await ops_test.run(
        "kubectl",
        "config",
        "view",
        check=True,
        fail_msg="Failed to execute kubectl - is kubectl installed?",
    )

    # Check if kubectl has a context, failing if it does not
    kubectl_config = yaml.safe_load(stdout)
    error_message = (
        "Found no kubectl contexts - did you populate KUBECONFIG?  Ex:"
        " 'KUBECONFIG=/home/runner/.kube/config tox ...' or"
        " 'KUBECONFIG=/home/runner/.kube/config tox ...'"
    )
    assert kubectl_config["contexts"] is not None, error_message

    await ops_test.run(
        "kubectl",
        "get",
        "pods",
        check=True,
        fail_msg="Failed to do a simple kubectl task - is KUBECONFIG properly configured?",
    )


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

    await ops_test.model.add_relation(
        f"{ISTIO_PILOT}:istio-pilot", f"{ISTIO_GATEWAY_APP_NAME}:istio-pilot"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )

@pytest.mark.abort_on_fail
async def test_deploy_bookinfo_example(ops_test: OpsTest):
    root_url = "https://raw.githubusercontent.com/istio/istio/release-1.11/samples/bookinfo"
    bookinfo_namespace = f"{ops_test.model_name}-bookinfo"

    await ops_test.run(
        "kubectl",
        "create",
        "namespace",
        bookinfo_namespace,
    )

    await ops_test.run(
        "kubectl",
        "label",
        "namespace",
        bookinfo_namespace,
        "istio-injection=enabled",
        "--overwrite=true",
        check=True,
    )
    await ops_test.run(
        "kubectl",
        "apply",
        "-f",
        f"{root_url}/platform/kube/bookinfo.yaml",
        "-f",
        f"{root_url}/networking/bookinfo-gateway.yaml",
        "--namespace",
        bookinfo_namespace,
        check=True,
    )

    await ops_test.run(
        "kubectl",
        "wait",
        "--for=condition=available",
        "deployment",
        "--all",
        "--all-namespaces",
        "--timeout=5m",
        check=True,
    )

    # Wait for the pods as well, since the Deployment can be considered
    # "complete" while the pods are still starting.
    await ops_test.run(
        "kubectl",
        "wait",
        "--for=condition=ready",
        "pod",
        "--all",
        f"-n={bookinfo_namespace}",
        "--timeout=5m",
        check=True,
    )

    gateway_ip = await get_gateway_ip(ops_test)
    await assert_page_reachable(
        url=f"https://{gateway_ip}/productpage", title="Simple Bookstore App"
    )

# TODO: Change this to use lightkube
async def get_gateway_ip(ops_test: OpsTest, service_name: str = "istio-ingressgateway-workload"):
    gateway_json = await ops_test.run(
        "kubectl",
        "get",
        f"services/{service_name}",
        "-n",
        ops_test.model_name,
        "-ojson",
        check=True,
    )

    gateway_obj = json.loads(gateway_json[1])
    return gateway_obj["status"]["loadBalancer"]["ingress"][0]["ip"]


@tenacity.retry(
    stop=tenacity.stop_after_delay(60),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def assert_url_get(url, allowed_statuses: list, disallowed_statuses: list):
    """Asserts that we receive one of a list of allowed status when we `get` an url, or raises.

    Raises after max number of attempts or if you receive a disallowed status code
    """
    i = 0
    max_attempts = 20
    while i < max_attempts:
        # Test that traffic over the restricted port (8080, the regular ingress)
        # is redirected to dex
        r = requests.get(url, allow_redirects=False)
        if r.status_code in allowed_statuses:
            return
        elif r.status_code in disallowed_statuses:
            raise ValueError(
                f"Got disallowed status code {r.status_code}.  Communication not as expected"
            )
        sleep(5)

    raise ValueError(
        "Timed out before getting an allowed status code.  Communication not as expected"
    )


# Use a long stop_after_delay period because wait_for_idle is not reliable.
@tenacity.retry(
    stop=tenacity.stop_after_delay(600),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def assert_page_reachable(url, title):
    """Asserts that a page with a specific title is reachable at a given url."""
    log.info(f"Attempting to access url '{url}' to assert it has title '{title}'")
    async with aiohttp.ClientSession(raise_for_status=True) as client:
        results = await client.get(url)
        soup = BeautifulSoup(await results.text())

    assert soup.title.string == title
    log.info(f"url '{url}' exists with title '{title}'.")


@tenacity.retry(
    stop=tenacity.stop_after_delay(600),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def assert_virtualservice_exists(name: str, namespace: str):
    """Will raise a ApiError(404) if the virtualservice does not exist."""
    log.info(f"Attempting to assert that  VirtualService '{name}' exists.")
    lightkube_client = lightkube.Client()
    lightkube_client.get(VIRTUAL_SERVICE_LIGHTKUBE_RESOURCE, name, namespace=namespace)
    log.info(f"VirtualService '{name}' exists.")
