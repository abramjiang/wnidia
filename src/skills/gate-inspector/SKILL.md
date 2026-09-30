---
name: gate-inspector
version: 1.0.0
description: 对 BP P21 的五条 GO/NO-GO 门槛做体检：给出每条门槛的实测值、目标、样本量与判定，样本不足时拒绝判 GO。当用户询问"我们到没到扩张门槛、该不该收缩、数据够不够判"时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Gate Inspector（GO / NO-GO 门槛体检）

## 何时使用
- 决定是否进入下一阶段（复制标杆 / 开放机主招募 / 批量出货）之前；
- 需要向投资人说明"我们的扩张是有量化门槛的，不达标就收缩"；
- 发现某项指标样本太少，需要明确"不能判 GO"。

## 类型化输入 / 输出
输入：`--ctrl`/`--token`（在线）或 `--state-file`（离线快照）。

输出（JSON）：
- `overall`：`go` / `watch` / `insufficient` / `no-go`；
- `gates[]`：每条门槛的 `value / target / comparator / sample / min_sample / verdict / reason / action`；
- `actions` 与 `per_gate_action`：收缩线对应的具体动作。

## 如何运行
```bash
python tool.py --live --ctrl http://127.0.0.1:9000
python tool.py --state-file evals/fixtures/state_sample.json
```

## 确定性规则（沿用 OpenSLO 的 objective/target/window 三段式）
- 每条门槛必须同时报出**目标值**、**窗口**与**样本量**；
- `sample < min_sample`（默认 5）→ `verdict=insufficient`，**不得判 GO**；
- 任一门槛 `no-go` → 整体 `no-go`，并给出该业务线的收缩动作；
- 成本类指标在离线快照下无法计算时只报样本量，不编造数值。

## 边界
不预测未来、不给收益承诺；目标值为团队设定（BP P21 原文标注为非行业基准）。
