from subprocess import CalledProcessError

import pytest

from istioctl import Istioctl, InstallFailedError, ManifestFailedError

ISTIOCTL_BINARY = "not_really_istioctl"
NAMESPACE = "dummy-namespace"
PROFILE = "my-profile"


def test_istioctl_install(subprocess):
    """Tests that istioctl.install() calls the binary successfully with the expected arguments."""
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    ictl.install()

    # Assert that we call istioctl with the expected arguments
    expected_call_args = [
        ISTIOCTL_BINARY,
        "install",
        "-y",
        "-s",
        PROFILE,
        "-s",
        f"values.global.istioNamespace={NAMESPACE}",
    ]

    subprocess.check_call.assert_called_once_with(expected_call_args)


@pytest.fixture()
def subprocess_failing(subprocess):
    cpe = CalledProcessError(
        cmd="",
        returncode=1,
        stderr="stderr",
        output="stdout"
    )

    for method_name in ("check_call", "check_output"):
        method = getattr(subprocess, method_name)
        method.side_effect = cpe
    yield subprocess


def test_istioctl_install_error(subprocess_failing):
    """Tests that istioctl.install() calls the binary successfully with the expected arguments."""
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    # Assert that we raise an error when istioctl fails
    with pytest.raises(InstallFailedError):
        ictl.install()


def test_istioctl_manifest(subprocess):
    raise NotImplementedError()


def test_istioctl_remove():
    raise NotImplementedError()
