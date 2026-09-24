#!/usr/bin/env bash
set -euo pipefail
bridge_root="$(cd "$(dirname "$0")" && pwd)"
source_root="${SIMPLE_SOURCE:-/home/fangqi/code/SIMPLE-o7-verify}"
cd "$bridge_root"
test -f .env || { echo 'Create .env with SIMPLE_O7_TOKEN first'; exit 1; }
mkdir -p runtime/home runtime/cache
if docker container inspect connection-simple-o7 >/dev/null 2>&1; then
  echo 'Container already exists. Use docker start connection-simple-o7 if stopped; inspect original commands before replacing it.'
  exit 1
fi
docker run -d --name connection-simple-o7 --init --gpus all \
  --cpus 4 --memory 8g --shm-size 1g \
  --user "$(id -u):$(id -g)" \
  -p 18770:18770 --env-file .env \
  -e HOME=/opt/connection-simple-o7/runtime/home \
  -e XDG_CACHE_HOME=/opt/connection-simple-o7/runtime/cache \
  -e TORCH_EXTENSIONS_DIR=/opt/connection-simple-o7/runtime/cache/torch_extensions \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/opt/connection-simple-o7:/mnt/simple/src:/mnt/simple \
  -e SIMPLE_RUNTIME_ROOT=/workspace/simple -e OMP_NUM_THREADS=2 \
  -v "$source_root:/mnt/simple:ro" \
  -v "$bridge_root:/opt/connection-simple-o7" \
  -w /opt/connection-simple-o7 \
  --entrypoint /workspace/simple/.venv/bin/python \
  simple:260904 -m service.server --config config.json
