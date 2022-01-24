import json
import logging
from asyncio import sleep

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

    # Wait for the pods as well, since the Deployment can be considered
    # "complete" while the pods are still starting.
    await ops_test.run(
        'kubectl',
        'wait',
        '--for=condition=ready',
        'pod',
        '--all',
        '-n=default',
        '--timeout=5m',
        check=True,
    )

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
    gateway_ip = gateway_obj['status']['loadBalancer']['ingress'][0]['ip']
    async with aiohttp.ClientSession(raise_for_status=True) as client:
        results = await client.get(f'http://{gateway_ip}/productpage')
        soup = BS(await results.text())

    assert soup.title.string == 'Simple Bookstore App'
