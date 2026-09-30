#!/usr/bin/env bash
# WNIDIA on DGX Spark 一键部署（在节点上、Developer 用户下执行）。
# 用法：
#   bash scripts/deploy_spark.sh                 # GPU 模式
#   MODE=mock bash scripts/deploy_spark.sh       # 无 GPU 的演示模式
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- 必须修改的安全项（部署红线：8888/9000 必须有鉴权，手册 8.2-9）----
# 不再提供 'changeme' / 'wnidia2026' 这类"能直接跑起来"的默认口令：
# 未显式注入时自动生成随机串，并只在终端脱敏回显（手册 8.1-3）。
gen_secret(){ python3 -c "import secrets;print(secrets.token_urlsafe(24))"; }
mask(){ python3 - "$1" <<'PY'
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ''
print('*' * len(s) if len(s) <= 8 else f'{s[:4]}{"*" * (len(s) - 8)}{s[-4:]}')
PY
}
if [ -z "${WNIDIA_TOKEN:-}" ]; then
  export WNIDIA_TOKEN="$(gen_secret)"
  echo "[info] 未提供 WNIDIA_TOKEN，已自动生成随机 Token（未回显明文，请自行保存）"
fi
export WNIDIA_DASH_USER="${WNIDIA_DASH_USER:-reviewer}"
if [ -z "${WNIDIA_DASH_PASS:-}" ]; then
  export WNIDIA_DASH_PASS="$(gen_secret)"
  echo "[info] 未提供 WNIDIA_DASH_PASS，已自动生成随机口令（未回显明文）"
fi
# 模型与引擎镜像（按节点实际路径/镜像覆盖）
export MODEL_PATH="${MODEL_PATH:-/models/stepfun}"
export MODELS_DIR="${MODELS_DIR:-/home/xsuper/models}"
export VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"

# ---- 预检：docker ----
if ! command -v docker >/dev/null 2>&1; then
  echo "[error] 未找到 docker；请确认节点已提供容器运行时，或改用本地 mock："
  echo "        bash scripts/run_local.sh"
  exit 1
fi
DC="docker compose"; docker compose version >/dev/null 2>&1 || DC="docker-compose"

if [ "${MODE:-gpu}" = "mock" ]; then
  COMPOSE="deploy/docker-compose.yml"
  echo "[mode] mock（无 GPU）"
else
  COMPOSE="deploy/docker-compose.gpu.yml"
  echo "[mode] gpu"
  # 可选 MPS（失败不阻断，多实例保底）
  bash deploy/mps-on.sh || true
fi

echo ">> 构建并启动： $COMPOSE"
$DC -f "$COMPOSE" up -d --build

echo ">> 等待控制面健康..."
for i in $(seq 1 20); do
  if curl -fsS -m 3 http://127.0.0.1:9000/healthz >/dev/null 2>&1; then
    echo "   控制面已就绪"; break
  fi
  sleep 3
done

echo
echo "============================================================"
echo "  WNIDIA 已在 DGX Spark 部署"
echo "  本机访问："
echo "    看板 http://127.0.0.1:8888   账号 $WNIDIA_DASH_USER / $(mask "$WNIDIA_DASH_PASS")"
echo "    API  http://127.0.0.1:9000   Token $(mask "$WNIDIA_TOKEN")"
echo "  公网访问（手册 1.2：只有 8888/9000 被映射；需 NODE_NUM 换算 8NN / 9NN）："
echo "    NODE_NUM=<51-100> 时：http://<PUBLIC_IP_2>:8NN（看板）/ :9NN（API）"
echo "    其余端口（7000 Agent / 8080 引擎）没有映射，请走 SSH 隧道："
echo "    ssh -p 6NN -L 7000:localhost:7000 -L 8080:localhost:8080 Developer@<PUBLIC_IP_2>"
echo " 演示（Token 见上方脱敏值对应的密码管理器记录）："
echo "    curl -X POST -H \"Authorization: Bearer \$WNIDIA_TOKEN\" \\"
echo "         http://127.0.0.1:9000/admin/inject/preempt"
echo " 合规自检："
echo "    curl -H \"Authorization: Bearer \$WNIDIA_TOKEN\" \\"
echo "         http://127.0.0.1:9000/admin/compliance"
echo " 压测："
echo "    python bench/load.py --target http://127.0.0.1:9000 \\"
echo "         --token \$WNIDIA_TOKEN --requests 60 --concurrency 8"
echo " 日志： $DC -f $COMPOSE logs -f controller"
echo " 停止： $DC -f $COMPOSE down"
echo "========================================================="
