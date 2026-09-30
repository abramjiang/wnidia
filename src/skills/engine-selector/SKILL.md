---
name: engine-selector
version: 1.0.0
description: 在多个推理引擎（mock / ollama / vllm / tensorfold）之间做确定性选路：结合任务类型、数据密级、节点档位与实时探活结果，给出目标引擎、评分依据、降级链与一致性保证。当用户询问"这个任务该用哪个引擎、为什么不用另一个、引擎挂了怎么办、能不能保证输出一致"时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Engine Selector（引擎选路决策）

## 何时使用
- 新增/替换推理后端时，先做一次"选路 dry-run"，向评审解释取舍依据；
- 机密任务（L3/L4）需要**可复现、可举证**的输出，必须确认所选引擎是否逐字节一致；
- 引擎探活失败时，给出明确的降级链，而不是静默失败或悄悄换模型；
- 答辩现场演示"引擎可插拔"：同一份权重的两条 ISA 路径（Apple Metal / NVIDIA CUDA）如何被调度面统一对待。

## 类型化输入 / 输出
输入：
- `task-type`（string）：`chat / heavy / batch / embed`；
- `secret`（string）：`L1 / L2 / L3 / L4`——L3 及以上强制要求一致性保证；
- `tier`（string）：`cloud / edge / home / cpu`；
- `prefer-exact`（flag）：即使密级不高，也要求逐字节可复现；
- `engines-source`（string，可选）：探活快照 JSON（离线评估用）；
- `ctrl / token / --live`（可选）：从控制面取实时探活结果。

输出（JSON）：
- `selected`（string）：目标引擎名；
- `score`（number）+ `reason`（array）：评分与可解释依据；
- `ranking`（array）：`{engine,score,healthy,exact,backend,reasons,caveats}` 全量排名；
- `fallback_chain`（array）：降级顺序；
- `consistency_required` / `consistency_satisfied`（bool）：是否要求一致性、是否已满足；
- `consistency_guarantee`（string）：一句话说明该引擎能给什么保证；
- `compliance`（object）：探活范围与绑定约束说明。

## 如何运行
```bash
# 离线：用探活快照评估，不需要任何服务
python tool.py --task-type chat --secret L3 --prefer-exact \
  --engines-source evals/fixtures/probes_healthy.json

# 在线：从控制面取真实探活结果
python tool.py --task-type heavy --secret L1 --live \
  --ctrl http://127.0.0.1:9000 --token "$WNIDIA_TOKEN"
```

## 确定性规则
- 排序：先按分数降序，**同分按固定优先级**（tensorfold > vllm > ollama > mock），保证可复现；
- 密级 L3/L4 或 `--prefer-exact` → 非 `exact` 引擎扣 25 分，并在 `consistency_satisfied` 上显式标注未满足；
- 探活失败 → 扣 60 分，排到降级链末尾（仍列出，便于解释"为什么不用它"）；
- 批处理类任务偏好多机张量并行与生产级连续批处理；在线交互偏好在不改变输出的前提下做草稿加速；
- 全部不可用 → 明确回落到 mock，并在 `reason` 中标注"模拟引擎，无真实 token"。

## 边界
- **只做决策**：不发请求、不改任何配置、不启动引擎；
- 探活仅限回环地址（`compliance.assert_probe_allowed` 强制），不触碰集群内网；
- 引擎一律只绑 `127.0.0.1`，对外由控制面 `:9000` 统一鉴权代理，其余端口走 SSH 隧道（手册第五章）；
- 真实吞吐与一致性需以真机 BENCHMARK 为准，本 Skill 只保证"选择逻辑可复现、可解释"。
