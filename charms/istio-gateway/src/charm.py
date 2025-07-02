#!/usr/bin/env python3

import logging
import re
from typing import Dict, Optional

from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from jinja2 import Environment, FileSystemLoader
from lightkube import Client, codecs
from lightkube.core.exceptions import ApiError
from ops import main
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, StatusBase, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces

logger = logging.getLogger(__name__)

SUPPORTED_GATEWAY_SERVICE_TYPES = ["LoadBalancer", "ClusterIP", "NodePort"]

METRICS_PATH = "/stats/prometheus"
METRICS_PORT = 9090

# Regex for Kubernetes annotation values:
# - Allows alphanumeric characters, dots (.), dashes (-), and underscores (_)
# - Matches the entire string
# - Does not allow empty strings
# - Example valid: "value1", "my-value", "value.name", "value_name"
# - Example invalid: "value@", "value#", "value space"
ANNOTATION_KEY_MAX_LENGTH = 253
ANNOTATION_VALUE_PATTERN = re.compile(r"^[\w.\-_]+$")
ANNOTATION_KEY_START_WITH = ("kubernetes.io/", "k8s.io/")

# Based on https://github.com/kubernetes/apimachinery/blob/v0.31.3/pkg/util/validation/validation.go#L204  # noqa
# Regex for DNS1123 subdomains:
# - Starts with a lowercase letter or number ([a-z0-9])
# - May contain dashes (-), but not consecutively, and must not start or end with them
# - Segments can be separated by dots (.)
# - Example valid: "example.com", "my-app.io", "sub.domain"
# - Example invalid: "-example.com", "example..com", "example-.com"
DNS1123_SUBDOMAIN_PATTERN = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)

# Based on https://github.com/kubernetes/apimachinery/blob/v0.31.3/pkg/util/validation/validation.go#L32  # noqa
# Regex for Kubernetes qualified names:
# - Starts with an alphanumeric character ([A-Za-z0-9])
# - Can include dashes (-), underscores (_), dots (.), or alphanumeric characters in the middle
# - Ends with an alphanumeric character
# - Must not be empty
# - Example valid: "annotation", "my.annotation", "annotation-name"
# - Example invalid: ".annotation", "annotation.", "-annotation", "annotation@key"
QUALIFIED_NAME_MAX_LENGTH = 63
QUALIFIED_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        # Every lightkube API call will use the model name as the namespace by default
        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")

        for event in [
            self.on.start,
            self.on["istio-pilot"].relation_changed,
            self.on.config_changed,
        ]:
            self.framework.observe(event, self.start)
        self.framework.observe(self.on.remove, self.remove)

        # metrics relation configuration
        self.prometheus_provider = MetricsEndpointProvider(
            charm=self,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "metrics_path": METRICS_PATH,
                    # Note(rgildein): Service is defined in manifest.yaml and without using full
                    # path, the grafana-agent will be using IP of application pod instead of IP
                    # of workload deployment.
                    "static_configs": [
                        {"targets": [f"istio-gateway-metrics.{self.model.name}.svc:{9090}"]}
                    ],
                }
            ],
        )

    @property
    def _workload_service_annotations(self) -> Optional[Dict[str, str]]:
        """Parse config and return dict of annotations for rendering the Workload Service.

        Annotations are passed as a configuration option in the form of key-value pairs,
        passed as a string to the charm: "key1=val1,key2=val2". This method manipulates
        the string to return a Dictionary with valid annotations (one key per annotation).
        """

        annotations = self.model.config["annotations"]
        if not annotations:
            return None

        annotations = annotations.strip().rstrip(",")
        try:
            dict_annotations = {
                key.strip(): value.strip()
                for key, value in (pair.split("=", 1) for pair in annotations.split(",") if pair)
            }
            return dict_annotations if valid_annotations(dict_annotations) else None
        except ValueError:
            logger.error(
                "Invalid format for annotations. " "Expected format: key1=val1,key2=val2."
            )
            return None

    def start(self, event):
        """Event handler for StartEevnt."""
        try:
            self._check_leader()

            interfaces = self._get_interfaces()

        except CheckFailed as error:
            self.model.unit.status = error.status
            return

        if self.model.config["kind"] not in ("ingress", "egress"):
            self.model.unit.status = BlockedStatus("Config item `kind` must be set")
            return

        if not self.model.relations["istio-pilot"]:
            self.model.unit.status = BlockedStatus("Please add required relation to istio-pilot")
            return

        if not ((pilot := interfaces["istio-pilot"]) and pilot.get_data()):
            self.model.unit.status = WaitingStatus(
                "Waiting for istio-pilot relation data, deferring event"
            )
            event.defer()
            return

        if self.model.config["gateway_service_type"] not in SUPPORTED_GATEWAY_SERVICE_TYPES:
            self.model.unit.status = BlockedStatus(
                f"Ingress Gateway Service must one of type: {SUPPORTED_GATEWAY_SERVICE_TYPES}"
            )
            return

        pilot = list(pilot.get_data().values())[0]

        env = Environment(loader=FileSystemLoader("src"))
        template = env.get_template("manifest.yaml")
        rendered = template.render(
            kind=self.model.config["kind"],
            namespace=self.model.name,
            proxy_image=self.model.config["proxy-image"],
            pilot_host=pilot["service-name"],
            pilot_port=pilot["service-port"],
            gateway_service_type=self.model.config["gateway_service_type"],
            replicas=self.model.config["replicas"],
            annotations=self._workload_service_annotations,
        )

        for obj in codecs.load_all_yaml(rendered):
            logger.debug(f"Deploying {obj.metadata.name} of kind {obj.kind}")
            self.lightkube_client.apply(obj, namespace=obj.metadata.namespace)

        self.unit.status = ActiveStatus()

    def remove(self, event):
        """Remove charm."""

        env = Environment(loader=FileSystemLoader("src"))
        template = env.get_template("manifest.yaml")
        rendered = template.render(
            kind=self.model.config["kind"],
            namespace=self.model.name,
            proxy_image=self.model.config["proxy-image"],
            pilot_host="foo",
            pilot_port="foo",
            replicas=self.model.config["replicas"],
        )

        try:
            for obj in codecs.load_all_yaml(rendered):
                self.lightkube_client.delete(
                    type(obj), obj.metadata.name, namespace=obj.metadata.namespace
                )
        except ApiError as err:
            logger.exception("ApiError encountered while attempting to delete resource.")
            if err.status.message is not None:
                if "(Unauthorized)" in err.status.message:
                    # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
                    logger.error(
                        f"Ignoring unauthorized error during cleanup:" f"\n{err.status.message}"
                    )
                else:
                    # But surface any other errors
                    logger.error(err.status.message)
                    raise
            else:
                raise

    def _check_leader(self):
        if not self.unit.is_leader():
            raise CheckFailed("Waiting for leadership", WaitingStatus)

    def _get_interfaces(self):
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise CheckFailed(str(err), WaitingStatus)
        except NoCompatibleVersions as err:
            raise CheckFailed(str(err), BlockedStatus)
        return interfaces


class CheckFailed(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=StatusBase):
        super().__init__()

        self.msg = str(msg)
        self.status_type = status_type
        self.status = status_type(self.msg)


def valid_annotations(annotations: Dict[str, str]) -> bool:
    """Return True if the annotations pass a series of checks, False otherwise.

    Validate the annotations against the following checks:
    * Length
    * Annotation syntax
    * Reserved prefixes

    Args:
      annotations (Dict[str,str]): dictionary with annotations
    """
    for key, value in annotations.items():
        return validate_annotation_key(key) and validate_annotation_value(value)
    return True


def is_qualified_name(value: str) -> bool:
    """Check if a value is a valid Kubernetes qualified name."""
    parts = value.split("/")
    if len(parts) > 2:
        return False  # Invalid if more than one '/'

    if len(parts) == 2:  # If prefixed
        prefix, name = parts
        if not prefix or not DNS1123_SUBDOMAIN_PATTERN.match(prefix):
            return False
    else:
        name = parts[0]  # No prefix

    if not name or len(name) > QUALIFIED_NAME_MAX_LENGTH or not QUALIFIED_NAME_PATTERN.match(name):
        return False

    return True


def validate_annotation_value(value: str) -> bool:
    """Validate the annotation value."""
    if not ANNOTATION_VALUE_PATTERN.match(value):
        logger.error(
            f"Invalid annotation value: '{value}'. Must follow Kubernetes annotation syntax."
        )
        return False

    return True


def validate_annotation_key(key: str) -> bool:
    """Validate the annotation key."""
    if len(key) > ANNOTATION_KEY_MAX_LENGTH:
        logger.error(f"Invalid annotation key: '{key}'. Key length exceeds 253 characters.")
        return False

    if not is_qualified_name(key.lower()):
        logger.error(f"Invalid annotation key: '{key}'. Must follow Kubernetes annotation syntax.")
        return False

    if key.startswith(ANNOTATION_KEY_START_WITH):
        logger.error(f"Invalid annotation: Key '{key}' uses a reserved prefix.")
        return False

    return True


if __name__ == "__main__":
    main(Operator)
