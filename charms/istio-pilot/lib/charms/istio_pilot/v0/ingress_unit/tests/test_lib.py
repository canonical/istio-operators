from textwrap import dedent
from unittest.mock import Mock

from ops.charm import CharmBase
from ops.model import Binding
from ops.testing import Harness

from charms.istio_pilot.v0.ingress_per_unit import IngressPerUnitRequirer
from charms.istio_pilot.v0.ingress_per_unit.testing import MockIPUProvider


class MockRequirerCharm(CharmBase):
    META = dedent(
        """\
        name: test-requirer
        requires:
          ingress-per-unit:
            interface: ingress-per-unit
            limit: 1
        """
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ipu = IngressPerUnitRequirer(self, port=80)


def test_ingress_requirer(monkeypatch):
    monkeypatch.setattr(Binding, "network", Mock(bind_address="10.10.10.10"))
    harness = Harness(MockRequirerCharm, meta=MockRequirerCharm.META)
    harness._backend.model_name = "test-model"
    harness.set_leader(False)
    harness.begin_with_initial_hooks()
    provider = MockIPUProvider(harness)

    assert not harness.charm.ipu.is_available()
    assert not harness.charm.ipu.is_ready()
    assert not harness.charm.ipu.is_failed()
    assert not provider.is_available()
    assert not provider.is_ready()
    assert not provider.is_failed()

    relation = provider.relate()
    provider.add_unit()
    assert harness.charm.ipu.is_available(relation)
    assert not harness.charm.ipu.is_ready(relation)
    assert not harness.charm.ipu.is_failed(relation)
    assert provider.is_available(relation)
    assert provider.is_ready(relation)
    assert not provider.is_failed(relation)

    request = provider.get_request(relation)
    assert request.units[0] is harness.charm.unit
    assert request.service == "test-charm"
    request.send_urls({harness.charm.unit: "http://url/"})
    assert harness.charm.ipu.is_available(relation)
    assert harness.charm.ipu.is_ready(relation)
    assert not harness.charm.ipu.is_failed(relation)
    assert harness.charm.ipu.urls == {"test-requirer/0": "http://url/"}
    assert harness.charm.ipu.unit_url == "http://url/"
