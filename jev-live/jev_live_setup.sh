#!/usr/bin/env bash
# =====================================================================
# JEV live 一键启用与验收脚本（路线 A / 路线 B）
#
#   路线 A（local）：进程内 transformers 加载开源 openJev 权重 —— 零改码，最贴合原设计
#   路线 B（multi）：用 Ollama 多模型交叉裁决 —— 无需 openJev 权重与外部服务
#
# 用法：
#   ./scripts/jev_live_setup.sh A      # 或 local
#   ./scripts/jev_live_setup.sh B      # 或 multi
#
# ⚠️ 重要：仅可在开发机 / 自备环境运行。
#    参赛节点部署通道已关闭（提交截止），禁止在该环境执行本脚本。
# =====================================================================
set -uo pipefail

ROUTE="${1:-A}"
case "$ROUTE" in
  A|a|local) ROUTE=A ;;
  B|b|multi) ROUTE=B ;;
  *) echo "用法: $0 A|local  或  B|multi"; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVFILE="$ROOT/scripts/jev_live.env"

echo "============================================================"
echo " JEV live 启用脚本 —— 路线 $ROUTE"
echo "============================================================"
echo "⚠️  仅可在开发机/自备环境运行；参赛节点已截止部署，禁止执行。"
echo

# ---------------- 路线 A ----------------
if [ "$ROUTE" = "A" ]; then
  echo "[A] 检查 Python 与依赖 ..."
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ]; then echo "  未找到 python3"; exit 1; fi
  echo "  python3: $PY"

  if "$PY" -c "import transformers" >/dev/null 2>&1; then
    echo "  transformers 已安装"
  else
    echo "  未安装 transformers —— 需执行（torch 较大，约 2GB，请确认磁盘）："
    echo "    $PY -m pip install transformers torch"
    echo "  安装后重跑本脚本。"
    exit 2
  fi

  cat > "$ENVFILE" <<EOF
# 路线 A：进程内 transformers 加载 openJev（真实 live 裁决）
export WNIDIA_JEV_MODE=auto
export WNIDIA_JEV_BACKEND=local
export WNIDIA_JEV_MODEL=heman10x/openJev-verdict-2.0
# 中文场景：默认 JEV 不自动信任（CJK 门控）；放开请设 1
# export WNIDIA_JEV_CJK_TRUST=1
EOF

  echo
  echo "  已生成环境文件: $ENVFILE"
  echo "  生效方式: source $ENVFILE"
  echo "  说明: 首次调用会自动下载 openJev 权重（151M ModernBERT）。"
fi

# ---------------- 路线 B ----------------
if [ "$ROUTE" = "B" ]; then
  echo "[B] 检查 Ollama ..."
  if ! command -v ollama >/dev/null 2>&1; then
    echo "  未找到 ollama —— 请先安装：https://ollama.com/download"
    echo "  安装后执行 ollama pull <模型> 至少准备两个模型。"
    exit 2
  fi
  echo "  ollama: $(command -v ollama)"

  echo "  本地模型："
  ollama list 2>/dev/null | sed 's/^/    /' || echo "    (无法列出，请确认 ollama 服务已启动)"

  CNT="$(ollama list 2>/dev/null | tail -n +2 | grep -c . || echo 0)"
  if [ "${CNT:-0}" -lt 2 ]; then
    echo
    echo "  ⚠️ 少于 2 个模型，多模型交叉需要至少两个。可执行（开发机）："
    echo "    ollama pull qwen2.5:0.5b"
    echo "  拉取后重跑本脚本。"
  fi

  cat > "$ENVFILE" <<EOF
# 路线 B：Ollama 多模型交叉裁决（真实 live 裁决，无需 openJev 权重）
export WNIDIA_JEV_MODE=live
export WNIDIA_JEV_BACKEND=multi
export WNIDIA_JEV_MULTI_BASE=http://127.0.0.1:11434
# 参与交叉裁决的模型，逗号分隔（请按本机实际模型修改）
export WNIDIA_JEV_MULTI_MODELS=qwen2.5:0.5b
# 相似度阈值
export WNIDIA_JEV_MULTI_THRESHOLD=0.60
# 注意：multi 的置信度=相似度，默认 JEV_CONF_AUTO=0.85 过高，建议下调
export WNIDIA_JEV_CONF_AUTO=0.60
# 中文场景放开 CJK 门控，才能产出自动裁决结论
export WNIDIA_JEV_CJK_TRUST=1
EOF

  echo
  echo "  已生成环境文件: $ENVFILE"
  echo "  ⚠️ 请先把 WNIDIA_JEV_MULTI_MODELS 改成你本机实际的 >=2 个模型。"
fi

# ---------------- 通用后续步骤 ----------------
cat <<EOF

============================================================
 后续步骤
============================================================
 1) 加载环境：
      source $ENVFILE

 2) 以 preview 模式启动（开发机）：
      cd $ROOT
      MODE=preview bash scripts/deploy_gx10.sh

 3) 触发任务后验证 JEV 报告：
      curl -s -H "Authorization: Bearer previewtoken" \\
        http://127.0.0.1:9000/admin/jev/report | head

============================================================
 验收标准（live 真正生效应看到）
============================================================
   summary.mode        :  mock  ->  live
   summary.live        :  0     ->  > 0
   dist.na 全量        :  是    ->  出现 ok / div
   consistency_ok      :  null  ->  1 / 0

 若中文内容下 dist 仍全为 na：
   检查 WNIDIA_JEV_CJK_TRUST=1 与 WNIDIA_JEV_CONF_AUTO（建议 0.60）。
EOF
