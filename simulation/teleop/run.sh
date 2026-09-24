#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$ROOT/venv/gpu/bin:$PATH"
export CUDAHOSTCXX="${CUDAHOSTCXX:-/usr/bin/g++-11}"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-8.6}"
cd "$ROOT"
exec "$ROOT/venv/gpu/bin/python" simulation/teleop/keyboard_control.py "$@"
