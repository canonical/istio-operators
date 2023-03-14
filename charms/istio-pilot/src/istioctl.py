import logging
import subprocess

import lightkube
from charmed_kubeflow_chisme.lightkube.batch import delete_many


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

    @property
    def _istioctl_flags(self):
        return [
            "-s",
            self._profile,
            "-s",
            f"values.global.istioNamespace={self._namespace}"
        ]

    def install(self):
        """Wrapper for the `istioctl install` command."""
        try:
            subprocess.check_call(
                [
                    self._istioctl_path,
                    "install",
                    "-y",
                    *self._istioctl_flags
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
                    *self._istioctl_flags
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

    def precheck(self) -> str:
        """Executes `istioctl x precheck` to validate whether the environment can be updated.

        Raises:
            subprocess.CalledProcessError: if the precheck command fails.
        """
        subprocess.check_call(
            [
                self._istioctl_path,
                "x",
                "precheck",
            ]
        )


    def remove(self):
        """Removes the Istio installation using istioctl and Lightkube.

        TODO: Should we use `istioctl x uninstall` here instead of lightkube?  It is an
        experimental feature but included in all istioctl versions we support.
        """
        manifest = self.manifest()

        # Render YAML into Lightkube Objects
        k8s_objects = lightkube.codecs.load_all_yaml(
            manifest, create_resources_for_crds=True
        )

        client = lightkube.Client()
        delete_many(
            client=client,
            objs=k8s_objects
        )

    def upgrade(self):
        """Upgrades the Istio installation using istioctl."""
        # TODO: Include the precheck here too
        # TODO: Robust error raising
        # TODO: Really test this one for the failure conditions.
        raise NotImplementedError()
