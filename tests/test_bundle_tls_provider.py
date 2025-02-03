from pathlib import Path

import lightkube
import pytest
import tenacity
import yaml
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Secret
from pytest_operator.plugin import OpsTest

ISTIO_GATEWAY_METADATA = yaml.safe_load(Path("charms/istio-gateway/metadata.yaml").read_text())
ISTIO_PILOT_METADATA = yaml.safe_load(Path("charms/istio-pilot/metadata.yaml").read_text())
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
ISTIO_PILOT_APP_NAME = "istio-pilot"
DEFAULT_GATEWAY_NAME = "test-gateway"
GATEWAY_RESOURCE = create_namespaced_resource(
    group="networking.istio.io",
    version="v1alpha3",
    kind="Gateway",
    plural="gateways",
)

SELF_SIGNED_CERTIFICATES = "self-signed-certificates"
SELF_SIGNED_CERTIFICATES_CHANNEL = "latest/edge"
SELF_SIGNED_CERTIFICATES_TRUST = True


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client()
    return client


@pytest.mark.abort_on_fail
async def test_build_and_deploy_istio_charms(ops_test: OpsTest, request):
    """Build and deploy istio-operators with TLS configuration."""
    istio_gateway_name = ISTIO_GATEWAY_METADATA["name"]
    istio_pilot_name = ISTIO_PILOT_METADATA["name"]
    if charms_path := request.config.getoption("--charms-path"):
        istio_gateway = (
            f"{charms_path}/{istio_gateway_name}/{istio_gateway_name}_ubuntu@20.04-amd64.charm"
        )
        istio_pilot = (
            f"{charms_path}/{istio_pilot_name}/{istio_pilot_name}_ubuntu@20.04-amd64.charm"
        )
    else:
        istio_gateway = await ops_test.build_charm("charms/istio-gateway")
        istio_pilot = await ops_test.build_charm("charms/istio-pilot")

    await ops_test.model.deploy(
        istio_pilot,
        application_name=ISTIO_PILOT_APP_NAME,
        config={"default-gateway": DEFAULT_GATEWAY_NAME},
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

    await ops_test.model.deploy(
        SELF_SIGNED_CERTIFICATES,
        channel=SELF_SIGNED_CERTIFICATES_CHANNEL,
    )

    await ops_test.model.add_relation(
        f"{ISTIO_PILOT_APP_NAME}:certificates", f"{SELF_SIGNED_CERTIFICATES}:certificates"
    )

    await ops_test.model.wait_for_idle(
        status="active",
        raise_on_blocked=False,
        timeout=90 * 10,
    )


@tenacity.retry(
    stop=tenacity.stop_after_delay(50),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=3),
    reraise=True,
)
@pytest.mark.abort_on_fail
def test_tls_configuration(lightkube_client, ops_test: OpsTest):
    """Check the Gateway and Secret are configured with TLS."""
    secret = lightkube_client.get(
        Secret, f"{DEFAULT_GATEWAY_NAME}-gateway-secret", namespace=ops_test.model_name
    )
    gateway = lightkube_client.get(
        GATEWAY_RESOURCE, DEFAULT_GATEWAY_NAME, namespace=ops_test.model_name
    )

    # Assert the Secret is not None and has correct values
    assert secret is not None
    assert secret.data["tls.crt"] is not None
    assert secret.data["tls.key"] is not None
    assert secret.type == "kubernetes.io/tls"

    # Assert the Gateway is correctly configured
    servers_dict = gateway.spec["servers"][0]
    servers_dict_port = servers_dict["port"]
    servers_dict_tls = servers_dict["tls"]

    assert servers_dict_port["name"] == "https"
    assert servers_dict_port["protocol"] == "HTTPS"

    assert servers_dict_tls["mode"] == "SIMPLE"
    assert servers_dict_tls["credentialName"] == secret.metadata.name
