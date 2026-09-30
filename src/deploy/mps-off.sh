#!/usr/bin/env bash
# 关闭 NVIDIA MPS。
set -eu
export CUDA_MPS_PIPE_DIRECTORY="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
if command -v nvidia-cuda-mps-control >/dev/null 2>&1; then
  echo quit | nvidia-cuda-mps-control 2>/dev/null || true
  echo "[mps] 已发送 quit，MPS 关闭"
else
  echo "[mps] 未安装 MPS 控制工具，无需关闭"
fi
