"""Charms dependencies for tests."""

from charmed_kubeflow_chisme.testing import CharmSpec

SELF_SIGNED_CERTIFICATES = CharmSpec(
    charm="self-signed-certificates",
    channel="latest/edge",
    trust=True,
)
DEX_AUTH = CharmSpec(
    charm="dex-auth",
    channel="2.41/edge",
    trust=True,
)
OIDC_GATEKEEPER = CharmSpec(
    charm="oidc-gatekeeper",
    channel="ckf-1.10/edge",
    trust=True,
)
TENSORBOARD_CONTROLLER = CharmSpec(
    charm="tensorboard-controller",
    channel="1.10/edge",
    trust=True,
)
KUBEFLOW_VOLUMES = CharmSpec(
    charm="kubeflow-volumes",
    channel="1.10/edge",
    trust=True,
)
