import logging
import subprocess
from typing import List, Optional

import lightkube.resources.policy_v1  # noqa: F401
import yaml


class IstioctlError(Exception):
    pass


class Istioctl:
    def __init__(
        self,
        istioctl_path: str,
        namespace: str = "istio-system",
        profile: str = "minimal",
        istioctl_extra_flags: Optional[List[str]] = None,
    ):
        """Wrapper for the istioctl binary.

        This class provides a python API for the istioctl binary, supporting install, upgrade,
        and other istioctl commands.

        Args:
            istioctl_path (str): Path to the istioctl binary to be wrapped
            namespace (str): The namespace to install Istio into
            profile (str): The Istio profile to use for installation or upgrades
            istioctl_extra_flags (optional, list): A list containing extra flags to pass to istioctl
        """
        self._istioctl_path = istioctl_path
        self._namespace = namespace
        self._profile = profile
        self._istioctl_extra_flags = (
            istioctl_extra_flags if istioctl_extra_flags is not None else []
        )

    @staticmethod
    def _remove_warning_lines_from_istioctl_version_output(command_output: str) -> str:
        """Remove all lines containing warnings from the command output of `istioctl version`."""
        all_lines = command_output.split("\n")
        lines_without_warnings = []
        for line in all_lines:
            if "warn" not in line.lower():
                lines_without_warnings.append()
        return "\n".join(lines_without_warnings)

    @property
    def _istioctl_flags(self):
        istioctl_flags = [
            "--set",
            f"profile={self._profile}",
            "--set",
            f"values.global.istioNamespace={self._namespace}",
        ]
        istioctl_flags.extend(self._istioctl_extra_flags)
        return istioctl_flags


    def install(self):
        """Wrapper for the `istioctl install` command."""
        install_msg = (
            "Installing the Istio Control Plane with the following settings:\n"
            f"Profile: {self._profile}\n"
            f"Namespace: {self._namespace}\n"
            f"Istioctl extra flags: {self._istioctl_extra_flags}"
        )

        try:
            logging.info(install_msg)
            subprocess.check_call([self._istioctl_path, "install", "-y", *self._istioctl_flags])
        except subprocess.CalledProcessError as cpe:
            error_msg = f"Failed to install istio using istioctl.  Exit code: {cpe.returncode}."
            logging.error(error_msg)
            logging.error(f"stdout: {cpe.stdout}")
            logging.error(f"stderr: {cpe.stderr}")

            raise IstioctlError(error_msg) from cpe

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

            raise IstioctlError(error_msg) from cpe

        return manifests

    def precheck(self):
        """Executes `istioctl x precheck` to validate whether the environment can be updated.

        NOTE: This function does not validate exact versions compatibility. This verification
        should be done by caller.

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
            raise IstioctlError(
                "Upgrade failed during `istio precheck` with error code" f" {cpe.returncode}"
            ) from cpe

    def remove(self):
        """Removes the Istio installation using istioctl.

        Raises:
            IstioctlError: if the istioctl uninstall subprocess fails
        """
        try:
            subprocess.check_call(
                [
                    self._istioctl_path,
                    "uninstall",
                    "--purge",
                    "-y",
                ]
            )
        except subprocess.CalledProcessError as cpe:
            raise IstioctlError(
                "Remove failed during `istioctl x uninstall --purge` with error code"
                f" {cpe.returncode}"
            ) from cpe

    def upgrade(self):
        """Upgrades the Istio installation using istioctl.

        Note that this only upgrades the control plane (eg: istiod), it does not upgrade the data
        plane (for example, the istio/proxyv2 image used in the istio-gateway charm).

        """
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
            raise IstioctlError(
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
            raise IstioctlError("Failed to get Istio version") from cpe

        version_string = self._remove_warning_lines_from_istioctl_version_output(
            version_string.decode("utf-8")
        )
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
        raise IstioctlError("Failed to get client version - no version found in output")
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
        raise IstioctlError(error_message_template.format(message="no control plane found"))

    if len(meshes) == 0:
        raise IstioctlError(error_message_template.format(message="no mesh found"))
    if len(meshes) > 1:
        raise IstioctlError(error_message_template.format(message="too many meshes found"))

    mesh = meshes[0]

    try:
        if mesh["Component"] != "pilot":
            raise IstioctlError(error_message_template.format(message="no control plane found"))
        version = mesh["Info"]["version"]
    except KeyError:
        raise IstioctlError(error_message_template.format(message="no control plane found"))

    return version
