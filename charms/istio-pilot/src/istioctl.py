import logging
import subprocess

import yaml
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from lightkube import Client, codecs


class InstallFailedError(Exception):
    pass


class ManifestFailedError(Exception):
    pass


class PrecheckFailedError(Exception):
    pass


class UpgradeFailedError(Exception):
    pass


class VersionCheckError(Exception):
    pass


class Istioctl:
    def __init__(
        self, istioctl_path: str, namespace: str = "istio-system", profile: str = "minimal"
    ):
        """Wrapper for the istioctl binary

        Args:
            binary_file (str): Path to the istioctl binary to be used
        """
        self._istioctl_path = istioctl_path
        self._namespace = namespace
        self._profile = profile

    @property
    def _istioctl_flags(self):
        return [
            "-s",
            f"profile={self._profile}",
            "-s",
            f"values.global.istioNamespace={self._namespace}",
        ]

    def install(self):
        """Wrapper for the `istioctl install` command."""
        try:
            subprocess.check_call([self._istioctl_path, "install", "-y", *self._istioctl_flags])
        except subprocess.CalledProcessError as cpe:
            error_msg = f"Failed to install istio using istioctl.  Exit code: {cpe.returncode}."
            logging.error(error_msg)
            logging.error(f"stdout: {cpe.stdout}")
            logging.error(f"stderr: {cpe.stderr}")

            raise InstallFailedError(error_msg) from cpe

    def manifest(self) -> str:
        """Wrapper for the `istioctl manifest generate` command.

        Returns:
            (str) a YAML string of the Kubernetes manifest for Istio
        """
        try:
            manifests = subprocess.check_output(
                [self._istioctl_path, "manifest", "generate", *self._istioctl_flags]
            )
        except subprocess.CalledProcessError as cpe:
            error_msg = (
                f"Failed to generate manifests for istio using istioctl. "
                f"Exit code: {cpe.returncode}."
            )
            logging.error(error_msg)
            logging.error(f"stdout: {cpe.stdout}")
            logging.error(f"stderr: {cpe.stderr}")

            raise ManifestFailedError(error_msg) from cpe

        return manifests

    def precheck(self):
        """Executes `istioctl x precheck` to validate whether the environment can be updated.

        Raises:
            PrecheckFailedError: if the precheck command fails.
        """
        try:
            subprocess.check_call(
                [
                    self._istioctl_path,
                    "x",
                    "precheck",
                ]
            )
        except subprocess.CalledProcessError as cpe:
            raise PrecheckFailedError(
                "Upgrade failed during `istio precheck` with error code" f" {cpe.returncode}"
            ) from cpe

    def remove(self):
        """Removes the Istio installation using istioctl and Lightkube.

        TODO: Should we use `istioctl x uninstall` here instead of lightkube?  It is an
        experimental feature but included in all istioctl versions we support.
        """
        manifest = self.manifest()

        # Render YAML into Lightkube Objects
        k8s_objects = codecs.load_all_yaml(manifest, create_resources_for_crds=True)

        client = Client()
        delete_many(client=client, objs=k8s_objects)

    def upgrade(self, precheck: bool = True):
        """Upgrades the Istio installation using istioctl.

        Note that this only upgrades the control plane (eg: istiod), it does not upgrade the data
        plane (for example, the istio/proxyv2 image used in the istio-gateway charm).

        Args:
            precheck (bool): Whether to run `self.precheck()` before upgrading
        """
        if precheck:
            self.precheck()

        try:
            subprocess.check_output(
                [
                    self._istioctl_path,
                    "upgrade",
                    "-y",
                    *self._istioctl_flags,
                ]
            )
        except subprocess.CalledProcessError as cpe:
            raise UpgradeFailedError(
                "Upgrade failed during `istioctl upgrade` with error code" f" {cpe.returncode}"
            ) from cpe

    def version(self) -> dict:
        """Returns istio client and control plane versions."""
        try:
            version_string = subprocess.check_output(
                [
                    self._istioctl_path,
                    "version",
                    f"-i={self._namespace}",
                    "-o=yaml",
                ]
            )
        except subprocess.CalledProcessError as cpe:
            raise VersionCheckError("Failed to get Istio version") from cpe

        version_dict = yaml.safe_load(version_string)
        return {
            "client": get_client_version(version_dict),
            "control_plane": get_control_plane_version(version_dict),
        }


def get_client_version(version_dict: dict) -> str:
    """Returns the client version from a dict of `istioctl version` output

    Args:
        version_dict (dict): A dict of the version output from `istioctl version -o yaml`

    Returns:
        (str) The client version
    """
    try:
        version = version_dict["clientVersion"]["version"]
    except (KeyError, TypeError):
        # TypeError in case version_dict is None
        raise VersionCheckError("Failed to get client version - no version found in output")
    return version


def get_control_plane_version(version_dict: dict) -> str:
    """Returns the control plane version from a dict of `istioctl version` output

    Args:
        version_dict (dict): A dict of the version output from `istioctl version -o yaml`

    Returns:
        (str) The control plane version
    """
    # Assert that we have only one mesh and it says it is a pilot.  Not sure how we can handle
    # multiple meshes here.
    error_message_template = "Failed to get control plane version - {message}"
    try:
        meshes = version_dict["meshVersion"]
    except KeyError:
        raise VersionCheckError(error_message_template.format(message="no control plane found"))

    if len(meshes) == 0:
        raise VersionCheckError(error_message_template.format(message="no mesh found"))
    if len(meshes) > 1:
        raise VersionCheckError(error_message_template.format(message="too many meshes found"))

    mesh = meshes[0]

    try:
        if mesh["Component"] != "pilot":
            raise VersionCheckError(
                error_message_template.format(message="no control plane found")
            )
        version = mesh["Info"]["version"]
    except KeyError:
        raise VersionCheckError(error_message_template.format(message="no control plane found"))

    return version
