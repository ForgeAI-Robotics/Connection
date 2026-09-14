#!/bin/bash
export PATH="/home/fangqi/WorkXCJ/gs_playground/.venv/bin:$PATH"
export CUDAHOSTCXX=/usr/bin/g++-11
export TORCH_CUDA_ARCH_LIST="8.6"

cd /home/fangqi/WorkXCJ/FQPlanner_Mujoco3DGSNew
python teleop/keyboard_control.py "$@"
