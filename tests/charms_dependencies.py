"""Charms dependencies for tests."""

from charmed_kubeflow_chisme.testing import CharmSpec

SELF_SIGNED_CERTIFICATES = CharmSpec(
    charm="self-signed-certificates",
    channel="latest/edge",
    trust=True,
)
DEX_AUTH = CharmSpec(
    charm="dex-auth",
    channel="latest/edge/pr-280",
    trust=True,
)
OIDC_GATEKEEPER = CharmSpec(
    charm="oidc-gatekeeper",
    channel="latest/edge",
    trust=True,
)
TENSORBOARD_CONTROLLER = CharmSpec(
    charm="tensorboard-controller",
    channel="latest/edge",
    trust=True,
)
KUBEFLOW_VOLUMES = CharmSpec(
    charm="kubeflow-volumes",
    channel="latest/edge",
    trust=True,
)
