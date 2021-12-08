import json
import logging
from asyncio import sleep
from pathlib import Path
from unittest.mock import ANY

import aiohttp
import pytest
from bs4 import BeautifulSoup as BS
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_deploy_bundle(ops_test: OpsTest):
    await ops_test.deploy_bundle(serial=True, extra_args=['--trust'])
    await ops_test.model.wait_for_idle(timeout=60 * 10)

    await ops_test.run(
        'kubectl',
        'wait',
        '--for=condition=available',
        'deployment',
        '--all',
        '--all-namespaces',
        '--timeout=5m',
        check=True,
    )
    for attempt in range(60):
        try:
            await ops_test.run(
                'kubectl',
                'get',
                'crd',
                'gateways.networking.istio.io',
                check=True,
            )
        except Exception:
            await sleep(1)
        else:
            break
    else:
        pytest.fail("Timed out waiting for Gateway CRD")

    root_url = 'https://raw.githubusercontent.com/istio/istio/release-1.11/samples/bookinfo'
    await ops_test.run(
        'kubectl',
        'label',
        'namespace',
        'default',
        'istio-injection=enabled',
        '--overwrite=true',
        check=True,
    )
    await ops_test.run(
        'kubectl',
        'apply',
        '-f',
        f'{root_url}/platform/kube/bookinfo.yaml',
        '-f',
        f'{root_url}/networking/bookinfo-gateway.yaml',
        check=True,
    )
    await ops_test.run(
        'kubectl',
        'wait',
        '--for=condition=available',
        'deployment',
        '--all',
        '--all-namespaces',
        '--timeout=5m',
        check=True,
    )

    for attempt in range(3):
        gateway_json = await ops_test.run(
            'kubectl',
            'get',
            'services/istio-ingressgateway',
            '-n',
            ops_test.model_name,
            '-ojson',
            check=True,
        )

        gateway_obj = json.loads(gateway_json[1])
        gateway_ingress = gateway_obj['status'].get('loadBalancer', {}).get('ingress')
        if gateway_ingress:
            gateway_ingress = gateway_ingress[0]
            ops_test.gateway_addr = gateway_ingress.get('host', gateway_ingress.get('ip'))
            break
        await sleep(10)
    else:
        pytest.fail("Timed out waiting for gateway load-balancer address")

    async with aiohttp.ClientSession(raise_for_status=True) as client:
        results = await client.get(f'http://{ops_test.gateway_addr}/productpage')
        soup = BS(await results.text())

    assert soup.title.string == 'Simple Bookstore App'


async def test_ingress(ops_test: OpsTest):
    await ops_test.run(
        'kubectl',
        'label',
        'namespace',
        ops_test.model_name,
        'istio-injection=enabled',
        '--overwrite=true',
        check=True,
    )

    base_path = Path(__file__).parent.parent
    ingress_lib_path = base_path / "charms/istio-pilot/lib/charms/istio_pilot/v0/ingress.py"
    ingress_charm_path = base_path / "tests/data/ingress-test"
    ingress_charm_path = ops_test.render_charm(
        ingress_charm_path,
        context={"ingress_lib": ingress_lib_path.read_text()},
    )
    ingress_charm = await ops_test.build_charm(ingress_charm_path)

    ingress_app = await ops_test.model.deploy(
        ingress_charm,
        num_units=3,
        resources={"httpbin-image": "kennethreitz/httpbin"},
    )
    await ops_test.model.wait_for_idle(raise_on_blocked=False)

    # finding the leader should not be this difficult
    status = await ops_test.model.get_status()
    units_status = status.applications["ingress-test"]["units"]
    ingress_leader = None
    for ingress_unit in ingress_app.units:
        assert ingress_unit.workload_status == "blocked"
        assert ingress_unit.workload_status_message == "Missing relation: ingress"
        if units_status[ingress_unit.name].get("leader", False):
            ingress_leader = ingress_unit
    assert ingress_leader is not None

    await ingress_app.add_relation("ingress", "istio-pilot:ingress")
    await ops_test.model.wait_for_idle(status="active", timeout=60)
    action = await ingress_leader.run_action("get-urls")
    output = await action.wait()
    assert output == {"status": "completed", "results": ANY}
    results = output.data["results"]
    assert results["url"] == f"http://{ops_test.gateway_addr}/ingress-test/"
    async with aiohttp.ClientSession(raise_for_status=True) as client:
        results = await client.get(results["url"] + "uuid")
        assert "uuid" in results.text()
        for unit_name, unit_url in results["unit_urls"].items():
            unit_num = unit_name.split("/")[-1]
            assert unit_url == f"http://{ops_test.gateway_addr}/ingress-test-unit-{unit_num}/"
            results = await client.get(unit_url + "uuid")
            assert "uuid" in results.text()
