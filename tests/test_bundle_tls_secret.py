import lightkube
import pytest
import tenacity
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Secret
from pytest_operator.plugin import OpsTest

ISTIO_PILOT = "istio-pilot"
ISTIO_GATEWAY_APP_NAME = "istio-ingressgateway"
DEFAULT_GATEWAY_NAME = "test-gateway"
GATEWAY_RESOURCE = create_namespaced_resource(
    group="networking.istio.io",
    version="v1alpha3",
    kind="Gateway",
    plural="gateways",
)


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client()
    return client


@pytest.mark.abort_on_fail
async def test_build_and_deploy_istio_charms(ops_test: OpsTest):
    """Build and deploy istio-operators with TLS configuration."""
    charms_path = "./charms/istio"
    istio_charms = await ops_test.build_charms(f"{charms_path}-gateway", f"{charms_path}-pilot")

    await ops_test.model.deploy(
        istio_charms["istio-pilot"],
        application_name=ISTIO_PILOT,
        config={"default-gateway": DEFAULT_GATEWAY_NAME},
        trust=True,
    )

    await ops_test.model.deploy(
        istio_charms["istio-gateway"],
        application_name=ISTIO_GATEWAY_APP_NAME,
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

    await run_save_tls_secret_action(ops_test)


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


async def run_save_tls_secret_action(ops_test: OpsTest):
    """Run the save-tls-secret action."""
    istio_pilot_unit = ops_test.model.applications[ISTIO_PILOT].units[0]
    istio_pilot_unit_action = await istio_pilot_unit.run_action(
        action_name="set-tls", **{"ssl-key": "key", "ssl-crt": "crt"}
    )
    await ops_test.model.get_action_output(action_uuid=istio_pilot_unit_action.entity_id, wait=120)
