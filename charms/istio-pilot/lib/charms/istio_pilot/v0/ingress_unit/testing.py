from schema_relation.testing import MockRemoteRelationMixin

from . import IngressPerUnitProvider, IngressPerUnitRequirer


class MockIPUProvider(MockRemoteRelationMixin, IngressPerUnitProvider):
    """Class to help with unit testing ingress requirer charms."""


class MockIPURequirer(MockRemoteRelationMixin, IngressPerUnitRequirer):
    """Class to help with unit testing ingress provider charms."""
