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
from charms_dependencies import DEX_AUTH, KUBEFLOW_VOLUMES, OIDC_GATEKEEPER, TENSORBOARD_CONTROLLER
from lightkube import codecs
from lightkube.generic_resource import (
    create_namespaced_resource,
    load_in_cluster_generic_resources,
)
from lightkube.resources.core_v1 import Pod
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


ISTIO_GATEWAY_METADATA = yaml.safe_load(Path("charms/istio-gateway/metadata.yaml").read_text())
ISTIO_PILOT_METADATA = yaml.safe_load(Path("charms/istio-pilot/metadata.yaml").read_text())
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
ISTIO_PILOT_APP_NAME = "istio-pilot"
ISTIO_RELEASE = "release-1.24"
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

    await ops_test.model.add_relation(
        f"{ISTIO_PILOT_APP_NAME}:istio-pilot", f"{ISTIO_GATEWAY_APP_NAME}:istio-pilot"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )


async def test_ingress_relation(ops_test: OpsTest):
    """Tests that the ingress relation works as expected, creating a route through the ingress.

    TODO (https://github.com/canonical/istio-operators/issues/259): Change this from using a
     specific charm that implements ingress's requirer interface to a generic charm
    """
    await ops_test.model.deploy(
        KUBEFLOW_VOLUMES.charm, channel=KUBEFLOW_VOLUMES.channel, trust=KUBEFLOW_VOLUMES.trust
    )

    await ops_test.model.add_relation(
        f"{ISTIO_PILOT_APP_NAME}:ingress", f"{KUBEFLOW_VOLUMES.charm}:ingress"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )

    assert_virtualservice_exists(name=KUBEFLOW_VOLUMES.charm, namespace=ops_test.model_name)

    # Confirm that the UI is reachable through the ingress
    gateway_ip = await get_gateway_ip(ops_test)
    # In KF, oidc-authservice adds the kubeflow-userid header once the request is authenticated.
    # Thus, every web app expect this header to be present.
    # See https://github.com/arrikto/oidc-authservice?tab=readme-ov-file#sequence-diagram-for-an-authentication-flow  # noqa: E501
    await assert_page_reachable(
        url=f"http://{gateway_ip}/volumes/",
        title="Frontend",
        headers={"kubeflow-userid": "random-user"},
    )


async def test_gateway_info_relation(ops_test: OpsTest):
    """Tests that the gateway-info relation works as expected.

    TODO (https://github.com/canonical/istio-operators/issues/259): Change this from using a
     specific charm that implements ingress's requirer interface to a generic charm
    """
    await ops_test.model.deploy(
        TENSORBOARD_CONTROLLER.charm,
        channel=TENSORBOARD_CONTROLLER.channel,
        trust=TENSORBOARD_CONTROLLER.trust,
    )

    await ops_test.model.add_relation(
        f"{ISTIO_PILOT_APP_NAME}:gateway-info", f"{TENSORBOARD_CONTROLLER.charm}:gateway-info"
    )

    # tensorboard_controller will go Active if the relation is established
    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
        idle_period=30,  # A hack because sometimes this proceeds without being Active
    )


@pytest.mark.abort_on_fail
async def test_deploy_bookinfo_example(ops_test: OpsTest):
    root_url = f"https://raw.githubusercontent.com/istio/istio/{ISTIO_RELEASE}/samples/bookinfo"
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
        url=f"http://{gateway_ip}/productpage", title="Simple Bookstore App"
    )


@pytest.mark.abort_on_fail
async def test_enable_ingress_auth(ops_test: OpsTest):
    """Tests that the ingress auth policy restricts traffic on (only the) kubeflow gateway.

    This test establishes the ingress-auth relation, which applies an auth policy to traffic
    through port 80/8080 on the gateway (the port opened for external traffic through the ingress).
    We test that unauthenticated traffic over port 80/8080 is restricted (eg: returns a 302 because
    it is redirected to dex).

    With the above auth restriction enabled, we also test that other routes through the gateway
    are not restricted.  This is tested by (through kubernetes manifests) creating a second
    ingress pathway using a different port (8081) in the istio-ingressgatway workload and
    verifying that traffic over that port does not get redirected to dex.  This test simulates
    similar behaviour to what is used in Knative's local gateway

    This test uses the bookinfo application from a previous test and must be run after that test.
    TODO:
    * Remove the bookinfo application, and just use a second simpler deployment
    * deploy/clean up deployments using fixtures?
    """
    # Deploy a secondary workload that also uses the istio proxy deployment (istio-ingressgateway),
    # but through a different port
    namespace = ops_test.model_name
    gateway_port = 8081
    test_workload_name = "secondary-ingress-test"
    deploy_workload_with_gateway(
        workload_name=test_workload_name, gateway_port=gateway_port, namespace=namespace
    )

    # Deploy everything needed to implement the ingress-auth relation
    regular_ingress_gateway_ip = await get_gateway_ip(ops_test)
    await ops_test.model.deploy(
        DEX_AUTH.charm,
        channel=DEX_AUTH.channel,
        trust=DEX_AUTH.trust,
        config={
            "static-username": USERNAME,
            "static-password": PASSWORD,
        },
    )

    await ops_test.model.deploy(
        OIDC_GATEKEEPER.charm,
        channel=OIDC_GATEKEEPER.channel,
        trust=OIDC_GATEKEEPER.trust,
    )
    await ops_test.model.integrate(f"{ISTIO_PILOT_APP_NAME}:ingress", f"{DEX_AUTH.charm}:ingress")
    await ops_test.model.integrate(
        f"{ISTIO_PILOT_APP_NAME}:ingress", f"{OIDC_GATEKEEPER.charm}:ingress"
    )
    await ops_test.model.integrate(
        f"{OIDC_GATEKEEPER.charm}:oidc-client", f"{DEX_AUTH.charm}:oidc-client"
    )
    await ops_test.model.integrate(
        f"{OIDC_GATEKEEPER.charm}:dex-oidc-config", f"{DEX_AUTH.charm}:dex-oidc-config"
    )
    await ops_test.model.integrate(
        f"{ISTIO_PILOT_APP_NAME}:ingress-auth", f"{OIDC_GATEKEEPER.charm}:ingress-auth"
    )

    # Wait for the oidc/dex charms to become active
    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=60 * 15,
    )

    # Wait for the pods from our secondary workload, just in case.  This should be faster than
    # the charms but maybe not.
    await ops_test.run(
        "kubectl",
        "wait",
        "--for=condition=ready",
        "pod",
        "--all",
        f"-n={namespace}",
        "--timeout=5m",
        check=True,
    )

    # Test that traffic over the restricted port (8080, the regular ingress) is redirected to dex
    assert_url_get(
        f"http://{regular_ingress_gateway_ip}/productpage",
        allowed_statuses=[302],
        disallowed_statuses=[200],
    )

    # Test that traffic over the secondary port (8081) is not redirected to dex
    secondary_gateway_ip = await get_gateway_ip(ops_test, f"{test_workload_name}-loadbalancer")
    assert_url_get(
        f"http://{secondary_gateway_ip}/test", allowed_statuses=[200], disallowed_statuses=[302]
    )


@pytest.mark.abort_on_fail
async def test_disable_ingress_auth(ops_test: OpsTest):
    """Tests that if we unrelate the ingress-auth relation, traffic is no longer restricted.

    Uses the previously deployed bookinfo application for testing.
    """
    await ops_test.model.applications[ISTIO_PILOT_APP_NAME].remove_relation(
        "ingress-auth", f"{OIDC_GATEKEEPER.charm}:ingress-auth"
    )

    # Wait for the istio-pilot charm to settle back down
    await ops_test.model.wait_for_idle(
        apps=[ISTIO_PILOT_APP_NAME],
        status="active",
        raise_on_blocked=False,
        raise_on_error=False,
        timeout=60 * 10,
    )

    gateway_ip = await get_gateway_ip(ops_test)
    await assert_page_reachable(
        url=f"http://{gateway_ip}/productpage", title="Simple Bookstore App"
    )


async def test_gateway_replicas_config_pod_anti_affinity(ops_test: OpsTest):
    """Test changing the replicas config to 2, and Assert the new Pod was not scheduled
    due to only 1 Node being available.
    """

    replicas_value = "2"
    await ops_test.model.applications[ISTIO_GATEWAY_APP_NAME].set_config(
        {"replicas": replicas_value}
    )
    await ops_test.model.wait_for_idle(apps=[ISTIO_GATEWAY_APP_NAME], status="active", timeout=300)

    client = lightkube.Client()

    # List gateway pods that are in Pending status
    pending_gateway_pods = list(
        client.list(
            Pod,
            namespace=ops_test.model_name,
            labels={"app": "istio-ingressgateway"},
            fields={"status.phase": "Pending"},
        )
    )

    # Assert one Pod is in Pending status
    assert len(pending_gateway_pods) == 1

    # Get the status message for the Pending pod
    pending_gateway_pod = pending_gateway_pods[0]
    message = pending_gateway_pod.status.conditions[0].message

    # Assert the status message is about anti-affinity
    assert "didn't match pod anti-affinity rules" in message


async def test_charms_removal(ops_test: OpsTest):
    """Test the istio-operators can be removed without errors."""
    # NOTE: the istio-gateway charm has to be removed before istio-pilot since
    # the latter contains all the CRDs that istio-gateway depends on.
    await ops_test.model.remove_application(ISTIO_GATEWAY_APP_NAME, block_until_done=True)
    await ops_test.model.remove_application(ISTIO_PILOT_APP_NAME, block_until_done=True)


def deploy_workload_with_gateway(workload_name: str, gateway_port: int, namespace: str):
    """Deploys an http server and opens a path through the existing istio proxy."""
    client = lightkube.Client()
    load_in_cluster_generic_resources(client)

    context = {
        "workload_name": workload_name,
        "namespace": namespace,
        "gateway_port": gateway_port,
    }
    manifest = codecs.load_all_yaml(
        Path("./tests/test_ingress_auth_manifest_for_setup.yaml.j2").read_text(), context=context
    )

    for r in manifest:
        client.create(r, r.metadata.name)


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
async def assert_page_reachable(url, title, headers: dict = {}):
    """Asserts that a page with a specific title is reachable at a given url."""
    log.info(f"Attempting to access url '{url}' to assert it has title '{title}'")
    async with aiohttp.ClientSession(raise_for_status=True) as client:
        results = await client.get(url=url, headers=headers)
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
