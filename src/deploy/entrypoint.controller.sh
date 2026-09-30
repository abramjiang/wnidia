#!/usr/bin/env bash
# 控制器容器入口：同时拉起 API(:9000) 与看板(:8888)，共享同一 SQLite。
set -eu
mkdir -p data

python -m uvicorn controller.main:app --host 0.0.0.0 --port 9000 &
API_PID=$!
python -m uvicorn controller.dash:app --host 0.0.0.0 --port 8888 &
DASH_PID=$!

trap 'kill $API_PID $DASH_PID 2>/dev/null || true' TERM INT
wait -n
exit $?
