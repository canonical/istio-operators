#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# dynamic list
IMAGE_LIST=()
IMAGE_LIST+=($(yq '.[] | .proxy-image.default' ./charms/istio-gateway/config.yaml))
PROXY_IMAGE=$(yq '.[] | .image-configuration.default' ./charms/istio-pilot/config.yaml | yq '.global-proxy-image')
PILOT_IMAGE=$(yq '.[] | .image-configuration.default' ./charms/istio-pilot/config.yaml | yq '.pilot-image')
HUB=$(yq '.[] | .image-configuration.default' ./charms/istio-pilot/config.yaml | yq '.global-hub')
TAG=$(yq '.[] | .image-configuration.default' ./charms/istio-pilot/config.yaml | yq '.global-tag')
IMAGE_LIST+=($(echo $HUB/$PROXY_IMAGE:$TAG))
IMAGE_LIST+=($(echo $HUB/$PILOT_IMAGE:$TAG))
printf "%s\n" "${IMAGE_LIST[@]}" | sort -u
