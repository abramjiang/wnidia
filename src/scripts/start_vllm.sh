#!/usr/bin/env bash
# 在 GX10 上用 ~/envs/vllm（vLLM 0.28.0 + torch 2.13）启动真实推理引擎。
# 用法：
#   bash scripts/start_vllm.sh                 # 单引擎 :8101
#   ENGINES=2 bash scripts/start_vllm.sh       # 双引擎 :8101 / :8102（模拟 GPU 切分）
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$HOME/envs/vllm/bin/python"
[ -x "$PY" ] || { echo "[error] 未找到 ~/envs/vllm/bin/python"; exit 1; }

MODEL="${MODEL:-$HOME/models/Nemotron-3.5-Lightning-30B-A3B-NVFP4}"
SERVED="${SERVED:-nemotron-nvfp4}"
N="${ENGINES:-1}"
LOGDIR="$ROOT/data/runlogs"; mkdir -p "$LOGDIR"

start_engine(){
  local port="$1" util="$2" tag="$3"
  echo ">> 启动引擎 $tag :$port  gpu_memory_utilization=$util"
  nohup "$PY" -m vllm.entrypoints.openai.api_server \
      --model "$MODEL" \
      --served-model-name "$SERVED" \
      --host 127.0.0.1 --port "$port" \
      --gpu-memory-utilization "$util" \
      --max-model-len "${MAXLEN:-4096}" \
      --trust-remote-code \
      >>"$LOGDIR/vllm-$tag.log" 2>&1 &
  echo "   日志: $LOGDIR/vllm-$tag.log"
}

start_engine 8101 "${UTIL0:-0.45}" e0
[ "$N" = "2" ] && start_engine 8102 "${UTIL1:-0.15}" e1

echo ">> 等待引擎就绪（首次加载 30B 权重约需数分钟）..."
for i in $(seq 1 90); do
  if curl -fsS -m 3 http://127.0.0.1:8101/v1/models >/dev/null 2>&1; then
    echo "   引擎就绪"; curl -s http://127.0.0.1:8101/v1/models | head -c 300; echo; break
  fi
  sleep 10
done

echo
echo "如引擎已就绪，切换 WNIDIA 到真实推理："
echo "  MODE=gpu ENGINE=vllm VLLM_BASE=http://127.0.0.1:8101/v1 VLLM_MODEL=$SERVED bash scripts/deploy_gx10.sh"
echo
echo "常见问题：30B NVFP4 加载失败/不支持时，改用 Ollama 引擎（已验证可用）："
echo "  MODE=gpu ENGINE=ollama bash scripts/deploy_gx10.sh"
