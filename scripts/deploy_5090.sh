#!/usr/bin/env bash
# WNIDIA 一键部署 —— NVIDIA RTX 5090 变体
#
# ⚠️ 重要声明：
#   本脚本依据 RTX 5090 的公开规格编写，**尚未在真实 5090 硬件上验证**。
#   首次运行请先以 MODE=mock 验证流程，再切 gpu 模式；如遇到问题请提 issue。
#   参赛提交版本仍基于 GB10（见 scripts/deploy_gx10.sh），本脚本为扩展路径。
#
# 与 GB10 版本的主要差异：
#   1. 不绑定任何固定公网 IP / SSH 账号（5090 多为自建或本地机器）
#   2. GPU_NAME 改为 RTX-5090，显存预算按 32GB 计（预留后默认 28GB）
#   3. 默认引擎改为 vLLM（更好批处理与吞吐，吃满 5090 的高带宽）
#   4. 支持 FP4 / FP8 量化（Blackwell）；Ollama 作为简易回退仍可选
#
# 前置（手动完成）：
#   - NVIDIA 驱动 + CUDA（Blackwell 需较新版本，按实际环境核对）
#   - python3 / tmux
#   - 若用 vLLM：pip install vllm（体积较大，首次耗时）
#   - 供电与散热：5090 满载约 575W，请确认电源与机箱风道
#
# 用法：
#   bash scripts/deploy_5090.sh                 # mock 模式（先跑通流程）
#   MODE=gpu bash scripts/deploy_5090.sh        # 真实 GPU（默认 vLLM）
#   MODE=gpu ENGINE=ollama bash scripts/deploy_5090.sh
#   WNIDIA_TOKEN=<强随机> WNIDIA_DASH_PASS=<强随机> MODE=gpu bash scripts/deploy_5090.sh
# 可选环境变量：
#   VRAM_BUDGET_GB   显存预算，默认 28（32GB 卡预留 4GB 给 KV/碎片）
#   QUANT            量化，默认 fp8（可选 fp4 / int8 / fp16）
#   PUBLIC_IP        对外访问地址，默认 127.0.0.1（本地）
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# ---- 5090 变体参数 ----
PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"
AGENT_PORT=7000
DASH_PORT=8888
API_PORT=9000
LOOPBACK=127.0.0.1

MODE="${MODE:-mock}"
ENGINE="${ENGINE:-vllm}"
VRAM_BUDGET_GB="${VRAM_BUDGET_GB:-28}"
QUANT="${QUANT:-fp8}"

export WNIDIA_DEPLOY_TARGET=rtx5090
export WNIDIA_MODE="${MODE}"
export WNIDIA_HOST=0.0.0.0
export WNIDIA_API_PORT="${API_PORT}"
export WNIDIA_DASH_PORT="${DASH_PORT}"
export WNIDIA_ENGINE="${WNIDIA_ENGINE:-auto}"
export WNIDIA_GPU_ENGINE="${ENGINE}"
export WNIDIA_TOKEN="${WNIDIA_TOKEN:-}"
export WNIDIA_DASH_USER="${WNIDIA_DASH_USER:-reviewer}"
export WNIDIA_DASH_PASS="${WNIDIA_DASH_PASS:-}"
export CTRL="http://${LOOPBACK}:${API_PORT}"
export WNIDIA_WORKER_HOST="${LOOPBACK}"
export WORKER_BIND="${LOOPBACK}"
export WNIDIA_JEV_MODE="${WNIDIA_JEV_MODE:-mock}"
export WNIDIA_JEV_BACKEND="${WNIDIA_JEV_BACKEND:-http}"
export WNIDIA_AGENT_POLICY="${WNIDIA_AGENT_POLICY:-off}"
# 设备画像（新增，供调度与计量使用）
export WNIDIA_VRAM_BUDGET_GB="${VRAM_BUDGET_GB}"
export WNIDIA_QUANT="${QUANT}"
export WNIDIA_GPU_NAME="RTX-5090"

LOGDIR="${ROOT}/data/runlogs"; mkdir -p "${LOGDIR}" "${ROOT}/data"
SESS=wnidia

mask(){ python3 - "$1" <<'PY'
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ''
print('*' * len(s) if len(s) <= 8 else f'{s[:4]}{"*" * (len(s) - 8)}{s[-4:]}')
PY
}
gen_secret(){ python3 -c "import secrets;print(secrets.token_urlsafe(24))"; }

echo "== WNIDIA on RTX 5090（未实机验证版；mode=${MODE}, engine=${ENGINE}）=="
echo "   显存预算 : ${VRAM_BUDGET_GB} GB    量化: ${QUANT}    对外: ${PUBLIC_IP}"

echo "== 0/6 凭据检查（应用层）=="
if [ -z "${WNIDIA_TOKEN}" ] || [ "${WNIDIA_TOKEN}" = "changeme" ]; then
  WNIDIA_TOKEN="$(gen_secret)"; export WNIDIA_TOKEN
  echo "    已自动生成随机 API Token（未回显，请自行保存）"
fi
if [ -z "${WNIDIA_DASH_PASS}" ] || [ "${WNIDIA_DASH_PASS}" = "wnidia2026" ]; then
  WNIDIA_DASH_PASS="$(gen_secret)"; export WNIDIA_DASH_PASS
  echo "    已自动生成随机看板密码（未回显）"
fi
echo "    API Token : $(mask "${WNIDIA_TOKEN}")"
echo "    看板口令  : $(mask "${WNIDIA_DASH_PASS}")"

# ---- 引擎端点（gpu 模式）----
if [ "${MODE}" = "gpu" ]; then
  if [ "${ENGINE}" = "vllm" ]; then
    export VLLM_BASE="${VLLM_BASE:-http://${LOOPBACK}:8100/v1}"
    export VLLM_MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
    echo "    vLLM 端点 : ${VLLM_BASE}（需自行先起 vLLM serve，端口 8100）"
  else
    export OLLAMA_HOST="${OLLAMA_HOST:-${LOOPBACK}:11434}"
    export VLLM_BASE="${VLLM_BASE:-http://${LOOPBACK}:11434/v1}"
    export VLLM_MODEL="${VLLM_MODEL:-qwen2.5:7b}"
    echo "    Ollama 端点: ${VLLM_BASE}"
  fi
fi
export WNIDIA_CALL_TIMEOUT="${WNIDIA_CALL_TIMEOUT:-360}"
export VLLM_TIMEOUT="${VLLM_TIMEOUT:-240}"

echo "== 1/6 引擎准备（mode=${MODE}）=="
if [ "${MODE}" = "gpu" ]; then
  if [ "${ENGINE}" = "ollama" ]; then
    if ! curl -fsS -m 3 "http://${LOOPBACK}:11434/api/version" >/dev/null 2>&1; then
      echo "    启动 ollama serve（仅 ${LOOPBACK}:11434）..."
      if command -v tmux >/dev/null 2>&1; then
        tmux new-session -d -s ollama "env OLLAMA_HOST=${OLLAMA_HOST} ollama serve" 2>/dev/null
      else
        nohup env OLLAMA_HOST="${OLLAMA_HOST}" ollama serve >>"${LOGDIR}/ollama.log" 2>&1 &
      fi
      for _ in $(seq 1 20); do
        curl -fsS -m 3 "http://${LOOPBACK}:11434/api/version" >/dev/null 2>&1 && break
        sleep 2
      done
    fi
  else
    if ! curl -fsS -m 3 "${VLLM_BASE}/models" >/dev/null 2>&1; then
      echo "    [warn] vLLM 未就绪（${VLLM_BASE}）。请先手动启动，例如："
      echo "      pip install vllm"
      echo "      vllm serve ${VLLM_MODEL} --port 8100 --gpu-memory-utilization 0.9 \\"
      echo "           --quantization ${QUANT} --max-model-len 8192"
      echo "    继续启动控制面，引擎就绪后即可正常工作。"
    fi
  fi
else
  echo "    mock 模式：不启动推理引擎"
fi

echo "== 2/6 清理旧进程 =="
tmux kill-session -t "${SESS}" 2>/dev/null && echo "    已结束旧 tmux 会话 ${SESS}"
pkill -f "controller.main" 2>/dev/null
pkill -f "controller.dash" 2>/dev/null
pkill -f "agent.app" 2>/dev/null
pkill -f "worker.agent" 2>/dev/null
sleep 1
if [ "${RESET:-0}" = "1" ]; then
  rm -f "${ROOT}"/data/wnidia.db*; echo "    已清空旧库（RESET=1）"
fi

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
if [ -x "${ROOT}/.venv/bin/python" ]; then PY="${ROOT}/.venv/bin/python"; fi
if ! "${PY}" -c "import fastapi, uvicorn, requests, pydantic" >/dev/null 2>&1; then
  echo "    安装依赖（用户目录 venv）..."
  [ -x "${ROOT}/.venv/bin/python" ] || "${PY}" -m venv "${ROOT}/.venv" 2>>"${LOGDIR}/pip.log"
  PY="${ROOT}/.venv/bin/python"
  "${PY}" -m pip install -q -r requirements.txt 2>>"${LOGDIR}/pip.log" \
    || { echo "[error] 依赖安装失败：${LOGDIR}/pip.log"; tail -8 "${LOGDIR}/pip.log"; exit 1; }
fi
echo "    依赖 OK：${PY}"

# ---- 环境变量落盘（权限 600）----
ENVFILE="${LOGDIR}/env.sh"
umask 077
cat > "${ENVFILE}" <<EOF
export WNIDIA_DEPLOY_TARGET=rtx5090
export WNIDIA_MODE=${MODE}
export WNIDIA_HOST=0.0.0.0
export WNIDIA_API_PORT=${API_PORT}
export WNIDIA_DASH_PORT=${DASH_PORT}
export WNIDIA_TOKEN=${WNIDIA_TOKEN}
export WNIDIA_DASH_USER=${WNIDIA_DASH_USER}
export WNIDIA_DASH_PASS=${WNIDIA_DASH_PASS}
export WNIDIA_WORKER_HOST=${LOOPBACK}
export WNIDIA_CALL_TIMEOUT=${WNIDIA_CALL_TIMEOUT}
export CTRL=http://${LOOPBACK}:${API_PORT}
export WORKER_BIND=${LOOPBACK}
export WNIDIA_ENGINE=${WNIDIA_ENGINE}
export WNIDIA_GPU_ENGINE=${ENGINE}
export WNIDIA_JEV_MODE="${WNIDIA_JEV_MODE:-mock}"
export WNIDIA_JEV_BACKEND="${WNIDIA_JEV_BACKEND:-http}"
export WNIDIA_AGENT_POLICY="${WNIDIA_AGENT_POLICY:-off}"
export WNIDIA_VRAM_BUDGET_GB=${VRAM_BUDGET_GB}
export WNIDIA_QUANT=${QUANT}
export WNIDIA_GPU_NAME=RTX-5090
export VLLM_TIMEOUT=${VLLM_TIMEOUT}
${VLLM_BASE:+export VLLM_BASE=${VLLM_BASE}}
${VLLM_MODEL:+export VLLM_MODEL=${VLLM_MODEL}}
EOF
chmod 600 "${ENVFILE}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "[error] 未找到 tmux，请先安装（apt install tmux / brew install tmux）"; exit 1
fi

echo "== 3/6 控制面 API（${API_PORT}）=="
tmux new-session -d -s "${SESS}" -n api \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; exec ${PY} -m uvicorn controller.main:app --host 0.0.0.0 --port ${API_PORT}'"
sleep 3

echo "== 4/6 看板（${DASH_PORT}）=="
tmux new-window -t "${SESS}" -n dash \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; exec ${PY} -m uvicorn controller.dash:app --host 0.0.0.0 --port ${DASH_PORT}'"

echo "== 5/6 Agent（${AGENT_PORT}）=="
tmux new-window -t "${SESS}" -n agent \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; exec ${PY} -m uvicorn agent.app:app --host 0.0.0.0 --port ${AGENT_PORT}'"

echo "== 6/6 worker（5090 画像：高带宽 / 中等容量）=="
tmux new-window -t "${SESS}" -n gpu \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; NODE=gpu-5090 NODE_ROLE=decode NODE_TIER=cloud MEM_LIMIT_GB=${VRAM_BUDGET_GB} COMPUTE_PCT=100 VLLM_PORT=8101 GPU_NAME=RTX-5090 exec ${PY} -m worker.agent'"
tmux new-window -t "${SESS}" -n cpu \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; NODE=cpu-1 NODE_ROLE=cpu NODE_TIER=cpu MEM_LIMIT_GB=4 COMPUTE_PCT=40 VLLM_PORT=8103 exec ${PY} -m worker.agent'"
sleep 5

echo "== 健康检查 =="
for _ in $(seq 1 15); do
  curl -fsS -m 3 "http://${LOOPBACK}:${API_PORT}/healthz" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS -m 3 "http://${LOOPBACK}:${API_PORT}/healthz" >/dev/null 2>&1 \
  && echo "   API OK（${API_PORT}）" || echo "   [error] API 未就绪，见 ${LOGDIR}"
code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "http://${LOOPBACK}:${DASH_PORT}/" || echo 000)
[ "$code" = "401" ] && echo "   看板 OK（401=Basic 鉴权）" || echo "   [warn] 看板 http_code=$code"
code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "http://${LOOPBACK}:${AGENT_PORT}/healthz" || echo 000)
[ "$code" = "200" ] && echo "   Agent OK（${AGENT_PORT}）" || echo "   [warn] Agent http_code=$code"

echo
echo "============================================================"
echo "  模式 : ${MODE}   引擎: ${VLLM_BASE:-mock}（${ENGINE}）"
echo "  看板 : http://${PUBLIC_IP}:${DASH_PORT}  （${WNIDIA_DASH_USER} / 看板密码）"
echo "  API  : http://${PUBLIC_IP}:${API_PORT}   （Authorization: Bearer <Token>）"
echo "  tmux : tmux attach -t ${SESS}（Ctrl+B 后按 D 脱离）"
echo "  窗口 : api / dash / agent / gpu / cpu"
echo "  停止 : tmux kill-session -t ${SESS}"
echo "  提醒 : 本变体尚未在真实 5090 上验证；参数请按实际环境调整"
echo "============================================================"
