#! /usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""Resources handling library using Lightkube."""
import logging

import yaml
from jinja2 import Environment, FileSystemLoader
from lightkube import Client, codecs
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource, GenericNamespacedResource


class ResourceHandler:
    def __init__(self, app_name, model_name):
        """A Lightkube API interface.

        Args:
           - app_name: name of the application
           - model_name: name of the Juju model this charm is deployed to
        """

        self.app_name = app_name
        self.model_name = model_name

        self.log = logging.getLogger(__name__)

        # Every lightkube API call will use the model name as the namespace by default
        self.lightkube_client = Client(namespace=self.model_name, field_manager="lightkube")

        self.env = Environment(loader=FileSystemLoader('src'))

    def delete_object(
        self, obj, namespace=None, ignore_not_found=False, ignore_unauthorized=False
    ):
        try:
            self.lightkube_client.delete(type(obj), obj.metadata.name, namespace=namespace)
        except ApiError as err:
            self.log.exception("ApiError encountered while attempting to delete resource.")
            if err.status.message is not None:
                if "not found" in err.status.message and ignore_not_found:
                    self.log.error(f"Ignoring not found error:\n{err.status.message}")
                elif "(Unauthorized)" in err.status.message and ignore_unauthorized:
                    # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
                    self.log.error(f"Ignoring unauthorized error:\n{err.status.message}")
                else:
                    self.log.error(err.status.message)
                    raise
            else:
                raise

    def delete_existing_resource_objects(
        self,
        resource,
        namespace=None,
        ignore_not_found=False,
        ignore_unauthorized=False,
        labels=None,
    ):
        if labels is None:
            labels = {}
        for obj in self.lightkube_client.list(
            resource,
            labels={"app.juju.is/created-by": f"{self.app_name}"}.update(labels),
            namespace=namespace,
        ):
            self.delete_object(
                obj,
                namespace=namespace,
                ignore_not_found=ignore_not_found,
                ignore_unauthorized=ignore_unauthorized,
            )

    def apply_manifest(self, manifest, namespace=None):
        for obj in codecs.load_all_yaml(manifest):
            self.lightkube_client.apply(obj, namespace=namespace)

    def delete_manifest(
        self, manifest, namespace=None, ignore_not_found=False, ignore_unauthorized=False
    ):
        for obj in codecs.load_all_yaml(manifest):
            self.delete_object(
                obj,
                namespace=namespace,
                ignore_not_found=ignore_not_found,
                ignore_unauthorized=ignore_unauthorized,
            )

    def generate_generic_resource_class(self, filename: str) -> GenericNamespacedResource:
        """Returns a class representing a namespaced K8s resource.

        Args:
            - filename: name of the manifest file used to create the resource
        """

        # TODO: this is a generic context that is used for rendering
        # the manifest files and extract their metadata. We should
        # improve how we do this and make it more generic.
        context = {
            'namespace': 'namespace',
            'app_name': 'name',
            'name': 'generic_resource',
            'request_headers': 'request_headers',
            'response_headers': 'response_headers',
            'port': 'port',
            'service': 'service',
        }

        t = self.env.get_template(filename)
        manifest = yaml.safe_load(t.render(context))
        ns_resource = create_namespaced_resource(
            group=manifest["apiVersion"].split("/")[0],
            version=manifest["apiVersion"].split("/")[1],
            kind=manifest["kind"],
            plural=f"{manifest['kind']}s".lower(),
            verbs=None,
        )

        return ns_resource
