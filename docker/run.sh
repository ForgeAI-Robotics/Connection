#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORK_ROOT="$(cd "${PROJECT_ROOT}/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-fqplanner-nav2:humble}"
CONTAINER_NAME="${CONTAINER_NAME:-fqplanner-nav2}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
DEFAULT_NAV_BRIDGE="${WORK_ROOT}/ros2_ws/src/fqplanner_nav_bridge"
if [ ! -d "${DEFAULT_NAV_BRIDGE}" ]; then
  DEFAULT_NAV_BRIDGE="${SCRIPT_DIR}/nav2/ros2_ws/src/fqplanner_nav_bridge"
fi
HOST_NAV_BRIDGE="${HOST_NAV_BRIDGE:-${DEFAULT_NAV_BRIDGE}}"
HOST_NAV2_DIR="${HOST_NAV2_DIR:-${PROJECT_ROOT}/nav2}"

docker run --rm -it \
  --name "${CONTAINER_NAME}" \
  --add-host=host.docker.internal:host-gateway \
  -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID}" \
  -e FQPLANNER_ROOT=/opt/fqplanner_nav \
  -v "${HOST_NAV_BRIDGE}:/opt/fqplanner_nav/ros2_ws/src/fqplanner_nav_bridge" \
  -v "${HOST_NAV2_DIR}:/opt/fqplanner_nav/nav2" \
  -p 5102:5102 \
  "${IMAGE_NAME}" \
  bash
