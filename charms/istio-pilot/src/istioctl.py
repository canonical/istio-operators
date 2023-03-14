import logging
import subprocess


class InstallFailedError(Exception):
    pass

class ManifestFailedError(Exception):
    pass


class Istioctl:
    def __init__(self, istioctl_path: str, namespace: str = "istio-system", profile: str = "minimal"):
        """Wrapper for the istioctl binary

        Args:
            binary_file (str): Path to the istioctl binary to be used
        """
        self._istioctl_path = istioctl_path
        self._namespace = namespace
        self._profile = profile

    def install(self):
        """Wrapper for the `istioctl install` command."""
        try:
            subprocess.check_call(
                [
                    self._istioctl_path,
                    "install",
                    "-y",
                    "-s",
                    self._profile,
                    "-s",
                    f"values.global.istioNamespace={self._namespace}",
                ]
            )
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
                [
                    self._istioctl_path,
                    "manifest",
                    "generate",
                    "-s",
                    self._profile,
                    "-s",
                    f"values.global.istioNamespace={self._namespace}",
                ]
            )
        except subprocess.CalledProcessError as cpe:
            error_msg = f"Failed to generate manifests for istio using istioctl. " \
                        f"Exit code: {cpe.returncode}."
            logging.error(error_msg)
            logging.error(f"stdout: {cpe.stdout}")
            logging.error(f"stderr: {cpe.stderr}")

            raise ManifestFailedError(error_msg) from cpe

        return manifests

    def remove(self):
        """Removes the istio installation using istioctl and Lightkube."""
        raise NotImplementedError()

