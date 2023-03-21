from contextlib import nullcontext as does_not_raise
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock

import pytest
from lightkube import ApiError

from istioctl import InstallFailedError, Istioctl, ManifestFailedError, PrecheckFailedError, UpgradeFailedError

ISTIOCTL_BINARY = "not_really_istioctl"
NAMESPACE = "dummy-namespace"
PROFILE = "my-profile"
EXAMPLE_MANIFEST = "./tests/unit/example_manifest.yaml"


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
        f"profile={PROFILE}",
        "-s",
        f"values.global.istioNamespace={NAMESPACE}",
    ]

    mocked_check_call.assert_called_once_with(expected_call_args)


@pytest.fixture()
def mocked_check_call_failing(mocked_check_call):
    cpe = CalledProcessError(cmd="", returncode=1, stderr="stderr", output="stdout")
    mocked_check_call.return_value = None
    mocked_check_call.side_effect = cpe

    yield mocked_check_call


@pytest.fixture()
def mocked_check_output_failing(mocked_check_output):
    cpe = CalledProcessError(cmd="", returncode=1, stderr="stderr", output="stdout")
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
        f"profile={PROFILE}",
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


@pytest.fixture()
def mocked_lightkube_client(mocker):
    mocked_lightkube_client = MagicMock()
    mocked_lightkube_client_class = mocker.patch("istioctl.Client")
    mocked_lightkube_client_class.return_value = mocked_lightkube_client

    yield mocked_lightkube_client


def test_istioctl_remove(mocked_check_output, mocked_lightkube_client):
    mocked_check_output.return_value = Path(EXAMPLE_MANIFEST).read_text()

    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    ictl.remove()

    assert mocked_lightkube_client.delete.call_count == 3


def test_istioctl_remove_error(mocked_check_output, mocked_lightkube_client, mocker):
    mocked_check_output.return_value = Path(EXAMPLE_MANIFEST).read_text()

    api_error = ApiError(response=mocker.MagicMock())
    mocked_lightkube_client.delete.return_value = None
    mocked_lightkube_client.delete.side_effect = api_error

    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    with pytest.raises(ApiError):
        ictl.remove()


def test_istioctl_precheck(mocked_check_call):
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    ictl.precheck()

    mocked_check_call.assert_called_once_with([ISTIOCTL_BINARY, "x", "precheck"])


def test_istioctl_precheck_error(mocked_check_call_failing):
    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    with pytest.raises(PrecheckFailedError):
        ictl.precheck()


def test_istioctl_upgrade(mocker, mocked_check_output):
    mocked_precheck = mocker.patch("istioctl.Istioctl.precheck")

    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    ictl.upgrade()

    assert mocked_precheck.call_count == 1
    mocked_check_output.assert_called_once_with(
        [
            ISTIOCTL_BINARY,
            "upgrade",
            "-y",
            "-s",
            f"profile={PROFILE}",
            "-s",
            f"values.global.istioNamespace={NAMESPACE}",
        ]
    )


def test_istioctl_upgrade_error(mocker, mocked_check_output_failing):
    mocker.patch("istioctl.Istioctl.precheck")

    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    with pytest.raises(UpgradeFailedError) as exception_info:
        ictl.upgrade()

    # Check if we failed for the right reason
    assert "istioctl upgrade" in exception_info.value.args[0]


def test_istioctl_upgrade_error_in_precheck(mocker):
    mocked_precheck = mocker.patch("istioctl.Istioctl.precheck")
    mocked_precheck.side_effect = PrecheckFailedError()

    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    with pytest.raises(PrecheckFailedError):
        ictl.upgrade()


def test_istioctl_upgrade_error_in_precheck_with_precheck_disabled(mocker):
    mocked_precheck = mocker.patch("istioctl.Istioctl.precheck")
    mocked_precheck.side_effect = CalledProcessError(returncode=1, cmd="")

    ictl = Istioctl(istioctl_path=ISTIOCTL_BINARY, namespace=NAMESPACE, profile=PROFILE)

    with does_not_raise():
        ictl.upgrade(precheck=False)
