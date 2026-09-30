---
name: private-cloud-planner
version: 1.0.0
description: 按并发路数、合规等级与交付形态产出私有云节点清单与三年 TCO，并把 vGPU/切分软件授权这一隐性成本单列。当用户询问"私有云怎么配、多少钱、为什么报价这么高"时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Private Cloud Planner（私有云配置与 TCO）

## 何时使用
- 客户给出并发路数与合规等级，需要一份可解释的配置与清单报价；
- 需要向客户证明"我们没有用硬件折扣掩盖后续年费"；
- 需要在 MIG/时间片（免授权）与 vGPU（按卡年费）之间做取舍。

## 类型化输入 / 输出
输入：`--concurrency`（典型 50–100 路）、`--grade`（L1–L4）、
`--gpus-per-node`、`--use-vgpu`、`--delivery`（lease/hosted/onprem）、`--years`。

输出（JSON）：
- `topology`：管理/CPU/GPU/存储/备份节点数、机柜数、冗余等级、25G 组网；
- `capex.items[]`：逐项单价与小计；
- `opex`：vGPU 授权年费、运维年费；
- `tco`：三年总额与每卡摊销；
- `hidden_cost_warning`：vGPU 授权的金额、影响与建议。

## 如何运行
```bash
python tool.py --concurrency 60 --grade L2 --use-vgpu --years 3
python tool.py --concurrency 100 --grade L3 --delivery hosted
```

## 确定性规则
- 配比经验值：每 25 路并发 1 个 GPU 节点，CPU 节点 3–6 个，存储 2–3 个；
- L3/L4 强制独立备份节点与双路管理；L4 额外标注"专属池"；
- `--use-vgpu` 时按 GPU 张数 × 年费计入 TCO，并置顶隐性成本提示；
- 单价可用环境变量覆盖，输出始终带 `assumption`。

## 边界
只做规划与测算，不含土建与机房造价；不代表已完成集成或已签约报价。
