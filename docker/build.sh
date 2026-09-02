#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="${IMAGE_NAME:-fqplanner-nav2:humble}"

docker build \
  -t "${IMAGE_NAME}" \
  -f "${SCRIPT_DIR}/nav2/Dockerfile" \
  "${SCRIPT_DIR}/nav2"
