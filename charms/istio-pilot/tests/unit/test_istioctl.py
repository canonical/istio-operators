from subprocess import CalledProcessError

import pytest

from istioctl import Istioctl, InstallFailedError, ManifestFailedError

ISTIOCTL_BINARY = "not_really_istioctl"
NAMESPACE = "dummy-namespace"
PROFILE = "my-profile"


def test_istioctl_install(mocked_check_call):
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

    mocked_check_call.assert_called_once_with(expected_call_args)


@pytest.fixture()
def mocked_check_call_failing(mocked_check_call):
    cpe = CalledProcessError(
        cmd="",
        returncode=1,
        stderr="stderr",
        output="stdout"
    )
    mocked_check_call.return_value = None
    mocked_check_call.side_effect = cpe

    yield mocked_check_call


@pytest.fixture()
def mocked_check_output_failing(mocked_check_output):
    cpe = CalledProcessError(
        cmd="",
        returncode=1,
        stderr="stderr",
        output="stdout"
    )
    mocked_check_output.return_value = None
    mocked_check_output.side_effect = cpe

    yield mocked_check_output


def test_istioctl_install_error(mocked_check_call_failing):
    """Tests that istioctl.install() calls the binary successfully with the expected arguments."""
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    # Assert that we raise an error when istioctl fails
    with pytest.raises(InstallFailedError):
        ictl.install()


def test_istioctl_manifest(mocked_check_output):
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    manifest = ictl.manifest()

    # Assert that we call istioctl with the expected arguments
    expected_call_args = [
        ISTIOCTL_BINARY,
        "manifest",
        "generate",
        "-s",
        PROFILE,
        "-s",
        f"values.global.istioNamespace={NAMESPACE}",
    ]

    mocked_check_output.assert_called_once_with(expected_call_args)

    # Assert that we received the expected manifests from istioctl
    expected_manifest = "stdout"
    assert manifest == expected_manifest


def test_istioctl_manifest_error(mocked_check_output_failing):
    """Tests that istioctl.install() calls the binary successfully with the expected arguments."""
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    # Assert that we raise an error when istioctl fails
    with pytest.raises(ManifestFailedError):
        ictl.manifest()


def test_istioctl_remove():
    raise NotImplementedError()


def test_istioctl_upgrade():
    raise NotImplementedError()
