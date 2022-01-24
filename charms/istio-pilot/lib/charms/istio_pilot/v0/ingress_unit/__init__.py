# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""# Ingress-per-Unit Interface Library.

TODO: Documentation
"""

# The unique Charmhub library identifier, never change it
LIBID = "b521889515b34432b952f75c21e96dfc"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

# flake8: noqa: E401,E402
from .provides import IngressUnitProvider
from .requires import IngressUnitRequirer
from . import testing
