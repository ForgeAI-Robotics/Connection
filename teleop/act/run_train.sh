#!/bin/bash
export PATH="/opt/robot/WorkXCJ/gs_playground/.venv/bin:$PATH"
export CUDAHOSTCXX=/usr/bin/g++-11
export TORCH_CUDA_ARCH_LIST="8.6"

cd /opt/robot/WorkXCJ/FQPlanner_Mujoco3DGSNew
python teleop/act/train.py "$@"
