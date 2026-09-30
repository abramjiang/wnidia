#!/usr/bin/env bash
# WNIDIA 沙盒测试环境：一条命令跑完全部离线套件（无需 Docker / GPU / pip）。
# 用法： bash scripts/run_all.sh
# 依赖：仅 Python3（本机无 pip/网络时，用 .stub/requests 替身 + .bin/python 垫片）。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- 垫片：真实 python3 + requests 替身（无网络安装时）----
REAL_PY="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || command -v python3)"
BIN="$ROOT/../.bin"
mkdir -p "$BIN"
ln -sf "$REAL_PY" "$BIN/python"
export PATH="$BIN:$PATH"
export PYTHONPATH="$ROOT/../.stub:${PYTHONPATH:-}"
export PYTHONDONTWRITEBYTECODE=1

run(){ echo; echo "===== $1 ====="; shift; "$@"; echo "  [exit=$?]"; }

run "1) Jev 决策层自检 (tests/jev_test.py)"        python3 tests/jev_test.py
run "2) 4 技能评测 (skills/run_evals.py)"           python3 skills/run_evals.py
run "3) 沙盒部署闭环 (scripts/sandbox.py)"          python3 scripts/sandbox.py
run "4) 收益测量 (bench/benefit.py)"                python3 bench/benefit.py

echo
echo "结果文件： data/sandbox_result.json · bench/benefit_result.json"
