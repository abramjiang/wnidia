---
name: smart-dispatcher
version: 1.0.0
description: 给定任务与节点画像，产出可解释的调度决策：准入判断、目标节点、评分、候选排名，以及是否需要抢占 spot。当用户询问“这个任务该发给谁、为什么选这个节点、要不要抢占、为什么被拒”时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Smart Dispatcher（智能分发决策）

## 何时使用
- 在真正派发前做一次“干跑（dry-run）”，向用户解释调度依据；
- 需要按 SLA、密级、数据主权、亲和性、利用率和信誉分综合选址；
- 判断高优任务是否应抢占低优 spot。

## 类型化输入 / 输出
输入：
- `task`（object）：`{prompt,task_type,sla,secret,tokens_in,need_mem_gb}`；
- `state-file`（string，可选）：节点快照 JSON（离线/评测用）；
- `ctrl/token`（string，可选）：在线控制面地址与 Token。

输出（JSON）：
- `admitted`（string）：`ok / no_capacity / sovereignty_violation`；
- `node`（string|null）；
- `score`（number）；
- `candidates`（array）：`{node,score,reasons}` 排名；
- `preempt`（object）：`{needed,spot_task}`；
- `reasons`（array<string>）：可解释依据。

## 如何运行
```bash
python tool.py --task-file evals/fixtures/task_chat.json \
  --state-file evals/fixtures/state.json
```

## 确定性规则
- 仅在 online、空闲显存达标、L2+ 同主权区、L3+ 可信节点中选择；
- 评分：亲和性 40 + 利用率余量 20 + 空闲显存 15 + KV 命中 10 + 信誉 15；
- S1 高优且最优节点被 S3 spot 占用 → `preempt.needed=true`；
- 无候选 → 给出明确拒绝原因而非静默失败。

## 边界
默认只产出决策（dry-run）；真正派发与抢占由控制面执行。
