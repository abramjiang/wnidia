---
name: gpu-slicer
version: 1.0.0
description: 根据 GPU 显存与工作负载，计算可执行的 GPU 切分方案（MIG / MPS / 多 vLLM 实例按 gpu_memory_utilization 切显存），产出实例配置、启动命令与失败回退策略。当用户询问“怎么把一张卡分给多个模型/任务、开多少实例、MPS 怎么配、显存怎么切”时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# GPU Slicer（GPU 切分优化）

## 何时使用
- 单台 DGX Spark / 单卡需要同时承载多个模型实例或租户；
- 需要在 MIG（GB10 不支持）、MPS（用户态增强）、多 vLLM 实例（保底核心）之间选型；
- 需要把切分结果转成可直接执行的启动命令与记账限流配置。

## 类型化输入 / 输出
输入：
- `gpu_mem_gb`（number）：GPU 总显存，DGX Spark GB10 为 128；
- `workload`（object）：`{"models":[{"name":string,"weights_gb":number,"kv_gb":number,"concurrency":int}]}`；
- `mode`（string）：`auto`（默认）/`mig`/`mps`/`instances`；
- `supports_mig`（bool，默认 false）、`supports_mps`（bool，默认 true）。

输出（JSON）：
- `selected_mode`（string）；
- `instances`（array）：`{id,name,mem_gb,gpu_memory_utilization,role,serve_command}`；
- `mps`（object）：`enabled` 与 `note`；
- `accounting`（object）：每实例显存额度与并发上限（供调度层记账限流）；
- `fallback`（array<string>）：失败回退路径。

## 如何运行
```bash
python tool.py --gpu-mem 128 \
  --workload '{"models":[{"name":"stepfun","weights_gb":40,"kv_gb":8,"concurrency":4}]}'
```

## 确定性规则
- MIG：仅当 `supports_mig=true` 且显式选择；GB10 默认不支持 → 自动回退；
- 总需求（权重+KV+预留）≤ 显存且为大量小并发 → MPS 叠加多实例；
- 否则按实例显存占比设置 `gpu_memory_utilization`，多实例共享，超额请求被调度层限流；
- 预留约 8% 显存给 CUDA context / 碎片。

## 边界
只产出配置与命令；实际拉起实例与限流由 deploy 脚本与 smart-dispatcher 执行。
