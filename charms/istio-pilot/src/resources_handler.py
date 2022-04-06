#! /usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""Resources handling library using Lightkube."""
import logging
from typing import Tuple, TextIO, Union, Iterable

import yaml
from jinja2 import Environment, FileSystemLoader
import lightkube  # noqa F401  # Needed for patching in test_resources_handler.py
from lightkube import Client, codecs
from lightkube.core.exceptions import LoadResourceError, httpx
from lightkube.generic_resource import create_namespaced_resource, GenericNamespacedResource
from lightkube.core.resource import Resource


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

    def delete_resource(
        self,
        obj,
        namespace=None,
        ignore_not_found=False,
    ):
        try:
            self.lightkube_client.delete(type(obj), obj.metadata.name, namespace=namespace)

        # For some reason, on 404 Not Found errors the raised exception
        # is httpx.HTTPStatusError instead of an ApiError
        # FIXME: find out why this is the case, probably something missing
        # in upstream lightkube
        except httpx.HTTPStatusError as err:
            self.log.warning(
                "HTTPStatusError encountered while attempting to delete resource.\n"
                "If ignore_not_found is set to True, this WARNING can be dismissed."
            )
            if err.response.status_code == 404 and ignore_not_found:
                self.log.warning(f"Ignoring not found error for: {obj.kind}/{obj.metadata.name}")
                return
            self.log.error(err)
            raise

    def delete_existing_resources(
        self,
        resource,
        namespace=None,
        ignore_not_found=False,
        labels=None,
    ):
        if labels is None:
            labels = {}
        for obj in self.lightkube_client.list(
            resource,
            labels={"app.juju.is/created-by": f"{self.app_name}"}.update(labels),
            namespace=namespace,
        ):
            self.delete_resource(
                obj,
                namespace=namespace,
                ignore_not_found=ignore_not_found,
            )

    def apply_manifest(self, manifest, namespace=None):
        for obj in codecs.load_all_yaml(manifest):
            self.lightkube_client.apply(obj, namespace=namespace)

    def delete_manifest(
        self,
        manifest,
        namespace=None,
        ignore_not_found=False,
        include_created_by_external=False,
    ):
        for obj in yaml.safe_load_all(manifest):
            if not obj:
                self.log.warning(
                    "Attempting to delete a resource from an empty resource object.\n"
                    "This could be due to an incorrectly formatted manifest."
                )
                return
            try:
                obj_to_delete = lightkube.codecs.from_dict(obj)
            except LoadResourceError:
                if include_created_by_external:
                    self.generate_generic_resource_class(manifest=obj)
                    obj_to_delete = lightkube.codecs.from_dict(obj)
                else:
                    raise
            finally:
                self.delete_resource(
                    obj_to_delete,
                    namespace=namespace,
                    ignore_not_found=ignore_not_found,
                )

    def generate_generic_resource_class(self, manifest: str = None) -> GenericNamespacedResource:
        manifest_dict = yaml.safe_load(manifest)

        # Get the CRD details from cluster itself, then create a resource class from that
        crd = self.lightkube_client.get(lightkube.CustomResourceDefinition, manifest_dict["kind"])
        if isNamespaced(crd):
            resource_creator = create_namespaced_resource
        else:
            resource_creator = create_generic_resource

        # Create the lightkube resource
        resource_class = resource_creator(**crd)

        return resource_class

    def reconcile_desired_resources(
        self,
        resource: GenericNamespacedResource,
        desired_resources: Union[str, TextIO, None],
        namespace: str = None,
    ) -> None:
        """Reconciles the desired list of resources of any kind.

        Args:
            resource: resource kind (e.g. Service, Pod)
            desired_resources: all desired resources in manifest form as str
            namespace: namespace of the resource
        """
        existing_resources = self.lightkube_client.list(
            resource,
            labels={
                "app.juju.is/created-by": f"{self.app_name}",
                f"app.{self.app_name}.io/is-workload-entity": "true",
            },
            namespace=namespace,
        )

        if desired_resources is not None:
            desired_resources_list = codecs.load_all_yaml(desired_resources)
            diff_obj = in_left_not_right(left=existing_resources, right=desired_resources_list)
            for obj in diff_obj:
                self.delete_resource(obj)
            self.apply_manifest(desired_resources, namespace=namespace)


def in_left_not_right(left, right):
    """Returns the resources in left that are not right
    Resources between left and right are deemed equal if they are the same resource type and name.
    Namespace is ignored as the desired resources are expected to be namespaced resources coming
    from templates that do not have namespace specified.
    """
    left_as_dict = resources_to_dict_of_resources(left)
    right_as_dict = resources_to_dict_of_resources(right)

    keys_in_left_not_right = set(left_as_dict.keys()) - set(right_as_dict.keys())
    in_left_not_right = [left_as_dict[k] for k in keys_in_left_not_right]

    return in_left_not_right


def select_resources_by_name(
    resource_list: Iterable[Resource], names: Iterable[str]
) -> Tuple[Resource]:
    """Returns the subset of the resources in resource_list that match the names defined in names
    Note this is a naive implementation that selects resources solely by name, ignoring things
    like resource type or namespace.
    Raises exceptions if:
    * any element of names is not in resource_list (raises KeyError)
    * resource_lists has multiple elements with the same name (raises KeyError)
    """
    resource_dict = {resource.metadata.name: resource for resource in resource_list}
    if len(resource_dict) != len(resource_list):
        raise ValueError(
            "Unexpected number of elements found during selection - are there "
            "multiple resources with the same metadata.name?"
        )
    selected = tuple(resource_dict[name] for name in names)
    return selected


def resources_to_dict_of_resources(resources):
    """Returns a dict of given Lightkube resources keyed by tuples of (kind, namespace, name)"""
    return {resource_to_tuple(r): r for r in resources}


def resource_to_tuple(resource: Resource) -> Tuple[Resource, Resource]:
    return resource.kind, resource.metadata.name
