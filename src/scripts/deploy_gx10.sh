#!/usr/bin/env bash
# WNIDIA 一键部署（实际分配的 gx10 节点：gx10-53e8 / 序号 76）
#
# 公网入口 : <PUBLIC_IP>   SSH 用户 <NODE_USER>   SSH 端口 <SSH_PORT>
# 端口映射（节点内网 -> 公网，2026-09-29 公网实测确认）：
#   7000 -> 7026   Agent（Bearer Token 鉴权）
#   8888 -> 8026   看板（HTTP Basic 鉴权）
#   9000 -> 9026   控制面 API（Bearer Token 鉴权）
#   说明：公网 8888/8026→8026 均不存在；公网 8026 应答的是看板（Basic 401），
#         公网 9026 才是 API（/healthz 返回 ok）。早期版本把 API 绑到 8026 是
#         错误推断，会导致公网 API 失联，已按实测映射修正。
# 其它端口（Ollama 11434、worker 8101-8103）只绑回环，用 SSH 隧道访问。
#
# 前置（你手动完成，脚本不接触 SSH 密码）：
#   ssh -p <SSH_PORT> <NODE_USER>@<PUBLIC_IP>
# 登录后，在 ~/wnidia 目录执行：
#   bash scripts/deploy_gx10.sh                 # mock 模式（无 GPU 依赖）
#   MODE=gpu bash scripts/deploy_gx10.sh        # 真实 GPU（Ollama，回环）
# 可预置应用凭据（不预置则自动生成强随机并提示保存）：
#   WNIDIA_TOKEN=<强随机> WNIDIA_DASH_PASS=<强随机> bash scripts/deploy_gx10.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# ---- gx10 固定参数 ----
PUBLIC_IP=<PUBLIC_IP>
SSH_USER=<NODE_USER>
SSH_PORT=<SSH_PORT>
AGENT_PORT=7000;  AGENT_PUB=7026
DASH_PORT=8888;   DASH_PUB=8026
API_PORT=9000;    API_PUB=9026
LOOPBACK=127.0.0.1

MODE="${MODE:-mock}"
ENGINE="${ENGINE:-ollama}"

export WNIDIA_DEPLOY_TARGET=gx10
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
# Agent 调度策略：off（默认）/ shadow（双轨留痕）/ enforce（提议裁决后采纳）
export WNIDIA_AGENT_POLICY="${WNIDIA_AGENT_POLICY:-off}"

LOGDIR="${ROOT}/data/runlogs"; mkdir -p "${LOGDIR}" "${ROOT}/data"
SESS=wnidia

mask(){ python3 - "$1" <<'PY'
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ''
print('*' * len(s) if len(s) <= 8 else f'{s[:4]}{"*" * (len(s) - 8)}{s[-4:]}')
PY
}
gen_secret(){ python3 -c "import secrets;print(secrets.token_urlsafe(24))"; }

echo "== 0/6 凭据检查（应用层；SSH 密码由你手动输入，脚本不接触）=="
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
echo "    节点      : gx10-53e8（序号76） 公网 ${PUBLIC_IP}"

# ---- 引擎（gpu 模式：Ollama 仅回环）----
if [ "${MODE}" = "gpu" ]; then
  export OLLAMA_HOST="${OLLAMA_HOST:-${LOOPBACK}:11434}"
  export VLLM_BASE="${VLLM_BASE:-http://${LOOPBACK}:11434/v1}"
  export VLLM_MODEL="${VLLM_MODEL:-modelscope.cn/unsloth/Qwen3.8-27B-GGUF:latest}"
fi
export WNIDIA_CALL_TIMEOUT="${WNIDIA_CALL_TIMEOUT:-360}"
export VLLM_TIMEOUT="${VLLM_TIMEOUT:-240}"

echo "== 1/6 引擎准备（mode=${MODE}, engine=${ENGINE}）=="
if [ "${MODE}" = "gpu" ]; then
  if ! curl -fsS -m 3 "http://${LOOPBACK}:11434/api/version" >/dev/null 2>&1; then
    echo "    启动 ollama serve（仅监听 ${LOOPBACK}:11434）..."
    if command -v tmux >/dev/null 2>&1; then
      tmux new-session -d -s ollama \
        "env OLLAMA_HOST=${OLLAMA_HOST} ollama serve" 2>/dev/null
    else
      nohup env OLLAMA_HOST="${OLLAMA_HOST}" ollama serve \
        >>"${LOGDIR}/ollama.log" 2>&1 &
    fi
    for _ in $(seq 1 20); do
      curl -fsS -m 3 "http://${LOOPBACK}:11434/api/version" >/dev/null 2>&1 && break
      sleep 2
    done
  fi
  echo "    Ollama: ${VLLM_BASE}  模型 ${VLLM_MODEL}（首次需 ollama pull ${VLLM_MODEL}）"
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
  echo "    安装依赖（用户目录 venv，无需 root）..."
  [ -x "${ROOT}/.venv/bin/python" ] || "${PY}" -m venv "${ROOT}/.venv" 2>>"${LOGDIR}/pip.log"
  PY="${ROOT}/.venv/bin/python"
  "${PY}" -m pip install -q -r requirements.txt 2>>"${LOGDIR}/pip.log" \
    || { echo "[error] 依赖安装失败：${LOGDIR}/pip.log"; tail -8 "${LOGDIR}/pip.log"; exit 1; }
fi
echo "    依赖 OK：${PY}"

# ---- 环境变量落盘（含凭据，权限 600）----
ENVFILE="${LOGDIR}/env.sh"
umask 077
cat > "${ENVFILE}" <<EOF
export WNIDIA_DEPLOY_TARGET=gx10
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
export VLLM_TIMEOUT=${VLLM_TIMEOUT}
${VLLM_BASE:+export OLLAMA_HOST=${OLLAMA_HOST:-}}
${VLLM_BASE:+export VLLM_BASE=${VLLM_BASE}}
${VLLM_MODEL:+export VLLM_MODEL=${VLLM_MODEL}}
EOF
chmod 600 "${ENVFILE}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "[error] 未找到 tmux（节点一般已预装；无 root 无法安装，请联系组委会）"; exit 1
fi

echo "== 3/6 控制面 API：内网 ${API_PORT}（公网 ${API_PUB}）=="
tmux new-session -d -s "${SESS}" -n api \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; exec ${PY} -m uvicorn controller.main:app --host 0.0.0.0 --port ${API_PORT}'"
sleep 3

echo "== 4/6 看板：内网 ${DASH_PORT}（公网 ${DASH_PUB}）=="
tmux new-window -t "${SESS}" -n dash \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; exec ${PY} -m uvicorn controller.dash:app --host 0.0.0.0 --port ${DASH_PORT}'"

echo "== 5/6 Agent：内网 ${AGENT_PORT}（公网 ${AGENT_PUB}）=="
tmux new-window -t "${SESS}" -n agent \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; exec ${PY} -m uvicorn agent.app:app --host 0.0.0.0 --port ${AGENT_PORT}'"

echo "== 6/6 worker（cloud-0 / edge-1 / cpu-1，仅回环）=="
tmux new-window -t "${SESS}" -n cloud \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; NODE=cloud-0 NODE_ROLE=prefill NODE_TIER=cloud MEM_LIMIT_GB=40 COMPUTE_PCT=100 VLLM_PORT=8101 GPU_NAME=GB10 exec ${PY} -m worker.agent'"
tmux new-window -t "${SESS}" -n edge \
  "bash -c 'source ${ENVFILE}; cd ${ROOT}; NODE=edge-1 NODE_ROLE=decode NODE_TIER=edge MEM_LIMIT_GB=8 COMPUTE_PCT=60 VLLM_PORT=8102 GPU_NAME=GB10 exec ${PY} -m worker.agent'"
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
[ "$code" = "401" ] && echo "   看板 OK（401=Basic 鉴权）" \
  || echo "   [warn] 看板 http_code=$code"
code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "http://${LOOPBACK}:${AGENT_PORT}/healthz" || echo 000)
[ "$code" = "200" ] && echo "   Agent OK（${AGENT_PORT}）" \
  || echo "   [warn] Agent http_code=$code"
echo "   端口绑定："
ss -tlnp 2>/dev/null | grep -E ":(${AGENT_PORT}|${DASH_PORT}|${API_PORT}|8101|8102|8103)\b" \
  | awk '{print "     "$4}' | sort -u

echo
echo "============================================================"
echo "  模式 : ${MODE}   引擎: ${VLLM_BASE:-mock}（${ENGINE}）"
echo "  公网看板 : http://${PUBLIC_IP}:${DASH_PUB}    （用户 ${WNIDIA_DASH_USER} / 看板密码）"
echo "  公网 API : http://${PUBLIC_IP}:${API_PUB}     （Authorization: Bearer <API Token>）"
echo "  公网Agent: http://${PUBLIC_IP}:${AGENT_PUB}   （Bearer <API Token>）"
echo "  SSH 隧道（Ollama/worker 调试）："
echo "    ssh -p ${SSH_PORT} -L 11434:localhost:11434 ${SSH_USER}@${PUBLIC_IP}"
echo "  tmux : tmux attach -t ${SESS}（Ctrl+B 后按 D 脱离）"
echo "  窗口 : api / dash / agent / cloud / edge / cpu"
echo "  停止 : tmux kill-session -t ${SESS}"
echo "  合规 : curl -H 'Authorization: Bearer <Token>' http://127.0.0.1:${API_PORT}/admin/compliance"
echo "  提醒 : 凭据不回显、勿截图外传；SSH 密码仅你手动输入"
echo "============================================================"
