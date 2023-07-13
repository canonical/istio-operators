#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# static list
STATIC_IMAGE_LIST=(
docker.io/istio/pilot:1.16.2
)
# dynamic list
IMAGE_LIST=()
IMAGE_LIST+=($(grep image charms/istio-gateway/src/manifest.yaml | awk '{print $2}' | sort --unique))
printf "%s\n" "${STATIC_IMAGE_LIST[@]}"
printf "%s\n" "${IMAGE_LIST[@]}"
