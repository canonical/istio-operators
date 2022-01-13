from textwrap import dedent

from ops.charm import CharmBase
from ops.testing import Harness

from charms.istio_pilot.v0.ingress import IngressRequirer, MockIngressProvider


class TestRequirerCharm(CharmBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ingress = IngressRequirer(self, port=80, per_unit_routes=True)


def test_ingress_requirer():
    harness = Harness(
        TestRequirerCharm,
        meta=dedent(
            """\
            name: test-requirer
            requires:
              ingress:
                interface: ingress
                limit: 1
            """
        ),
    )
    provider = MockIngressProvider(harness)
    harness.set_leader(True)
    harness.begin_with_initial_hooks()

    assert not harness.charm.ingress.is_available
    assert not harness.charm.ingress.is_ready

    provider.relate()
    assert harness.charm.ingress.is_available
    assert not harness.charm.ingress.is_ready

    provider.respond()
    assert harness.charm.ingress.is_available
    assert harness.charm.ingress.is_ready
    assert harness.charm.ingress.url == "http://mock-ingress-provider/test-requirer/"
    assert harness.charm.ingress.unit_url == "http://mock-ingress-provider/test-requirer-unit-0/"
