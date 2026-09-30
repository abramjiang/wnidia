---
name: resource-doctor
version: 1.0.0
description: 诊断 GPU/节点健康与利用率，检查 MIG/MPS 状态、心跳、空闲显存与异常节点，产出结构化诊断报告与可执行修复建议。当用户询问“为什么慢、节点是否正常、利用率多少、有没有掉线/作弊节点”时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Resource Doctor（算力体检）

## 何时使用
- 需要盘点集群/单机伪分布式下各节点状态、利用率、空闲显存、信誉分；
- 定位掉线节点、心跳超时、低空闲显存、作弊/低信誉节点；
- 在演示前做一次“健康巡检”。

## 类型化输入 / 输出
输入（可选，均有默认值）：
- `ctrl`（string）：控制面地址，默认 `http://127.0.0.1:9000`；
- `token`（string）：Bearer Token，默认读取环境变量 `WNIDIA_TOKEN`，再默认 `changeme`。

输出（JSON 对象）：
- `summary`（object）：`nodes_total / online / lost / avg_util / low_mem_nodes`；
- `findings`（array）：每条 `{severity: "high"|"medium"|"low", code: string, message: string}`；
- `recommendations`（array<string>）：可执行修复动作；
- `nodes`（array）：节点明细快照。

## 如何运行
```bash
python tool.py --ctrl http://127.0.0.1:9000 --token changeme
```

## 判定规则（确定性）
- 心跳超时/状态非 online → `high`，建议重路由并排查；
- 空闲显存 < 1GB → `medium`，建议切分/扩容；
- 利用率持续 < 10% 且有任务 → `low`，建议纳管闲置；
- 信誉分 < 0.9 或 cheat=true → `high`，建议隔离复核。

## 边界
只读，不修改任何节点状态；修复动作交由 gpu-slicer / idle-onboarding / smart-dispatcher 执行。
