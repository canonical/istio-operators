#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Library for sharing Istio Gateway(s) information

This library offers a Python API for providing and requesting information about
Istio Gateway(s) by wrapping the `gateway-info` relation endpoints.
The default relation name is `gateway-info` and it's recommended to use that name,
though if changed, you must ensure to pass the correct name when instantiating the
provider and requirer classes, as well as in metadata.yaml.
 
## Getting Started

### Fetching the library with charmcraft

Using charmcraft you can:
```shell
charmcraft fetch-lib charms.istio_pilot.v0.istio_gateway_info
```

## Using the library as requirer

### Add relation to metadata.yaml
```yaml
requires:
  gateway-info:
    interface: istio-gateway-info
    limit: 1
```

### Instantiate the GatewayRequirer class in charm.py

```python
from charms.istio_pilot.v0.istio_gateway_info import GatewayRequirer, GatewayRelationError

Class RequirerCharm(CharmBase):
    def __init__(self, *args):
        self.gateway = GatewayRequirer(self)
        self.framework.observe(self.on.some_event_emitted, self.some_event_function)

    def some_event_function():
        # use the getter function wherever the info is needed
        try:
            gateway_data = self.gateway_relation.get_relation_data()
        except GatewayRelationError as error:
            raise <your preferred exception> from error
```

## Using the library as provider

### Add relation to metadata.yaml
```yaml
provides:
  gateway-info:
    interface: istio-gateway-info
```

### Instantiate the GatewayProvider class in charm.py

```python
from charms.istio_pilot.v0.istio_gateway_info import GatewayProvider, GatewayRelationError
class ProviderCharm(self):
    def __init__(self, *args, **kwargs):
        ...
        self.gateway_provider = GatewayProvider(self)
        self.observe(self.on.some_event, self._some_event_handler)
    def _some_event_handler(self, ...):
        # This will update the relation data bag with the Gateway name and namespace
        try:
            self.gateway_provider.send_gateway_data(charm, gateway_name, gateway_namespace)
        except GatewayRelationError as error:
            raise <your preferred exception with a message> from error
```

## Relation data

The data shared by this library has only two items:

* gateway_name: the name of the Gateway the provider knows about. It corresponds to
the `name` field in the Gateway definition:

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: Gateway
metadata:
  name: my-gateway
```

* gateway_namespace: the namespace where the Gateway is deployed.
"""

import logging
from ops.framework import Object
from ops.model import Model, Relation
from ops.charm import CharmBase

# The unique Charmhub library identifier, never change it
LIBID = "354103422e7a43e2870e4203fbb5a649"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2

# Default relation and interface names. If changed, consistency must be kept
# across the provider and requirer.
DEFAULT_RELATION_NAME = "gateway-info"
DEFAULT_INTERFACE_NAME = "istio-gateway-info"

logger = logging.getLogger(__name__)


class GatewayRelationError(Exception):
    pass


class GatewayRelationMissingError(GatewayRelationError):
    def __init__(self):
        self.message = "Missing gateway-info relation."
        super().__init__(self.message)


class GatewayRelationDataMissingError(GatewayRelationError):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class GatewayRequirer(Object):
    """Base class that represents a requirer relation end.

    Args:
        requirer_charm (CharmBase): the requirer application
        relation_name (str, optional): the name of the relation

    Attributes:
        requirer_charm (CharmBase): variable for storing the requirer application
        relation_name (str): variable for storing the name of the relation
    """

    def __init__(self, requirer_charm, relation_name: str = DEFAULT_RELATION_NAME):
        super().__init__(requirer_charm, relation_name)
        # TODO: remove this attribute, we are not really using it at all.
        # Leaving it to keep backwards compatibility.
        self.requirer_charm = requirer_charm
        self.relation_name = relation_name

    @staticmethod
    def _relation_preflight_checks(relation: Relation) -> None:
        """Series of checks for the relation and relation data.
        Args:
            relation (Relation): the relation object to run the checks on
        Raises:
            GatewayRelationDataMissingError: if data is missing or incomplete
            GatewayRelationMissingError: if there is no related application
        """
        # Raise if there is no related applicaton
        if not relation:
            raise GatewayRelationMissingError()

        # Extract remote app information from relation
        remote_app = relation.app
        # Get relation data from remote app
        relation_data = relation.data[remote_app]

        # Raise if there is no data found in the relation data bag
        if not relation_data:
            raise GatewayRelationDataMissingError("No data found in relation data bag.")

        # Check if the relation data contains the expected attributes
        expected_attributes = ["gateway_name", "gateway_namespace"]
        missing_attributes = [
            attribute for attribute in expected_attributes if attribute not in relation_data
        ]
        if missing_attributes:
            raise GatewayRelationDataMissingError(f"Missing attributes: {missing_attributes}")

    def get_relation_data(self) -> dict:
        """Returns a dictionary with the Gateway information.

        Raises:
            GatewayRelationDataMissingError: if data is missing entirely or some attributes
            GatewayRelationMissingError: if there is no related application
        """
        # Run pre-flight checks
        relation = self.model.get_relation(self.relation_name)
        self._relation_preflight_checks(relation=relation)

        # Get relation data from remote app
        relation_data = relation.data[relation.app]

        return {
            "gateway_name": relation_data["gateway_name"],
            "gateway_namespace": relation_data["gateway_namespace"],
        }


class GatewayProvider(Object):
    """Base class that represents a provider relation end.

    Args:
        provider_charm (CharmBase): the provider application
        relation_name (str, optional): the name of the relation

    Attributes:
        provider_charm (CharmBase): variable for storing the provider application
        relation_name (str): variable for storing the name of the relation
    """

    def __init__(self, provider_charm, relation_name: str = DEFAULT_RELATION_NAME):
        super().__init__(provider_charm, relation_name)
        self.provider_charm = provider_charm
        self.relation_name = relation_name

    def send_gateway_relation_data(self, gateway_name: str, gateway_namespace: str) -> None:
        """Updates the relation data bag with data from the localGateway.
        Args:
            gateway_name (str): the name of the Gateway the provider knows about
            gateway_namespace(str): the namespace of the Gateway the provider knows about
        """
        # Update the relation data bag with localgateway information
        relations = self.model.relations[self.relation_name]

        # Raise if there is no related applicaton
        if not relations:
            raise GatewayRelationMissingError()

        # Update relation data
        for relation in relations:
            relation.data[self.provider_charm.app].update(
                {
                    "gateway_name": gateway_name,
                    "gateway_namespace": gateway_namespace,
                }
            )
