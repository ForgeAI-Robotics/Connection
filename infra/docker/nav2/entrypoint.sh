#!/usr/bin/env bash
set -e

source /opt/ros/humble/setup.bash
source /opt/fqplanner_nav/ros2_ws/install/setup.bash

export FQPLANNER_ROOT="${FQPLANNER_ROOT:-/opt/fqplanner_nav}"

exec "$@"
