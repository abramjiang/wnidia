#!/usr/bin/env bash
# WNIDIA v4 本地一键启动（无需 Docker/GPU）。
# 启动：API :9000、看板 :8888、Agent :7000、worker（cloud/edge/cpu，可选 home）。
#
# 环境变量：
#   BIND=0.0.0.0            暴露 8888/9000（需配强 Token，否则合规自检会拒绝启动）
#   WNIDIA_EXTRA_NODES=1    额外起 home-1 端侧节点（:8104），用于三层演示与沙盒
#   WNIDIA_PY=<python>      指定解释器
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${WNIDIA_PY:-python3}"
BIND="${BIND:-127.0.0.1}"
LOOPBACK=127.0.0.1
EXTRA="${WNIDIA_EXTRA_NODES:-0}"

export WNIDIA_TOKEN="${WNIDIA_TOKEN:-changeme}"
export WNIDIA_WORKER_HOST=127.0.0.1
export CTRL=http://127.0.0.1:9000
export WORKER_BIND="$LOOPBACK"
export NODE_NUM="${NODE_NUM:-}"
LOGDIR="$ROOT/data/runlogs"; mkdir -p "$LOGDIR"

[ "${RESET_DB:-1}" = "1" ] && rm -f "$ROOT"/data/wnidia.db*

PIDS=()
start(){ echo ">> $1"; shift; "$@" & PIDS+=($!); }

start "API :9000" "$PY" -m uvicorn controller.main:app --host "$BIND" --port 9000
sleep 3
start "看板 :8888" "$PY" -m uvicorn controller.dash:app --host "$BIND" --port 8888
sleep 1
start "Agent :7000" "$PY" -m uvicorn agent.app:app --host "$LOOPBACK" --port 7000
sleep 1

# 中心层：prefill，具备 CC-L2（可出证明）
NODE=cloud-0 NODE_ROLE=prefill NODE_TIER=cloud MEM_LIMIT_GB=40 \
  COMPUTE_PCT=100 VLLM_PORT=8101 GPU_NAME=GB10 \
  NODE_FORM_FACTOR=server NODE_GENERATION=gb10 NODE_CC_LEVEL=CC-L2 \
  NODE_BANDWIDTH_MBPS=10000 NODE_POWER_W=0 \
  start "worker cloud-0 :8101" "$PY" -m worker.agent
# 边缘层：算力箱形态
NODE=edge-1 NODE_ROLE=decode NODE_TIER=edge MEM_LIMIT_GB=8 \
  COMPUTE_PCT=60 VLLM_PORT=8102 GPU_NAME=Jetson-Thor \
  NODE_FORM_FACTOR=box NODE_GENERATION=thor NODE_CC_LEVEL=CC-L0 \
  NODE_BANDWIDTH_MBPS=2500 NODE_POWER_W=100 \
  start "worker edge-1 :8102" "$PY" -m worker.agent
# 内部兜底档（不对外披露，见 capabilities.TIER_TO_LAYER）
NODE=cpu-1 NODE_ROLE=cpu NODE_TIER=cpu MEM_LIMIT_GB=4 \
  COMPUTE_PCT=40 VLLM_PORT=8103 \
  NODE_FORM_FACTOR=server NODE_GENERATION=x86 NODE_BANDWIDTH_MBPS=1000 \
  start "worker cpu-1 :8103" "$PY" -m worker.agent

if [ "$EXTRA" = "1" ]; then
  # 端侧层：家庭异构节点（低带宽、受限功耗）
  NODE=home-1 NODE_ROLE=decode NODE_TIER=home MEM_LIMIT_GB=16 \
    COMPUTE_PCT=50 VLLM_PORT=8104 GPU_NAME=Apple-M4 \
    NODE_FORM_FACTOR=mini NODE_GENERATION=m4 NODE_CC_LEVEL=CC-L0 \
    NODE_BANDWIDTH_MBPS=300 NODE_POWER_W=35 \
    start "worker home-1 :8104" "$PY" -m worker.agent
fi

cleanup(){ echo; echo ">> 停止全部服务"; kill "${PIDS[@]}" 2>/dev/null; sleep 1; kill -9 "${PIDS[@]}" 2>/dev/null; exit 0; }
trap cleanup INT TERM

echo
echo "============================================================"
echo "  WNIDIA v4 已启动（本地 mock 模式，解释器 $PY）"
echo "  看板:  http://127.0.0.1:8888   （Basic 鉴权）"
echo "  API :  http://127.0.0.1:9000   （Bearer Token）"
echo "  门户:  http://127.0.0.1:9000/portal  （只读自助页）"
echo "  Agent: http://127.0.0.1:7000/?token=<Token>   （仅回环）"
echo "  节点 :  cloud-0:8101  edge-1:8102  cpu-1:8103$([ "$EXTRA" = "1" ] && echo "  home-1:8104")"
echo "  凭据一律不回显（手册 8.1-3）"
echo "  沙盒 :  WNIDIA_PY=$PY python scripts/sandbox_v4.py"
echo "  按 Ctrl-C 停止全部服务"
echo "========================================================="
wait
