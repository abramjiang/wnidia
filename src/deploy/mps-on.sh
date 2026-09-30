#!/usr/bin/env bash
# 在 DGX Spark 主机上启用 NVIDIA MPS（用户态共享，可选增强）。
# 需要在启动 GPU 容器之前执行；不支持/失败时直接使用多实例保底。
set -eu
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export CUDA_MPS_PIPE_DIRECTORY="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
export CUDA_MPS_LOG_DIRECTORY="${CUDA_MPS_LOG_DIRECTORY:-/var/log/nvidia-mps}"
mkdir -p "$CUDA_MPS_PIPE_DIRECTORY" "$CUDA_MPS_LOG_DIRECTORY"

if ! command -v nvidia-cuda-mps-control >/dev/null 2>&1; then
  echo "[mps] 未找到 nvidia-cuda-mps-control，跳过（多实例保底，不影响核心功能）"
  exit 0
fi
if pgrep -x nvidia-cuda-mps-control >/dev/null 2>&1; then
  echo "[mps] MPS 守护进程已在运行"
  exit 0
fi
nvidia-cuda-mps-control -d
echo "[mps] MPS 已启用，pipe=$CUDA_MPS_PIPE_DIRECTORY"
