#!/usr/bin/env bash
# [已合并] 本入口自 v5.1-gx10 起并入 scripts/deploy_gx10.sh。
#
# 历史：v5.1 时代本脚本固化"节点 26"参数（API 9000→9026、看板 8888→8026、
# Agent 仅回环）。gx10 适配后，deploy_gx10.sh 直接固化实测映射
# （7000→7026 / 8888→8026 / 9000→9026）并默认 MODE=gpu 可选，
# 两个入口的端口方案曾出现矛盾（API 8026 vs 9000），为消除歧义只保留一个入口。
#
# 本文件仅作兼容转发：原样透传全部环境变量后执行 deploy_gx10.sh。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 兼容旧用法：node26 入口历史上默认真实推理（gpu + Ollama），保留该默认
export MODE="${MODE:-gpu}"
export ENGINE="${ENGINE:-ollama}"
echo "[deploy_node26] 已并入 deploy_gx10.sh，正在转发（MODE=${MODE}，端口以实测映射为准）..."
exec bash "${ROOT}/scripts/deploy_gx10.sh" "$@"
