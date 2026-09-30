---
name: idle-onboarding
version: 1.0.0
description: 纳管一台闲置/新增节点：先探活，再向控制面注册，建立数字孪生画像、初始信誉分与配额，输出纳管结果与下一步。当用户询问“怎么把一台新机器/闲置 GPU 加进来、节点怎么注册、初始配额和信誉怎么定”时使用。
metadata:
  requires:
    bins: ["python"]
  type: operational
---

# Idle Onboarding（闲置纳管）

## 何时使用
- 发现闲置 GPU / 新增边缘或家用节点，希望纳入统一调度；
- 需要在注册前确认节点存活、角色与显存；
- 需要为新节点初始化画像、信誉分（默认 1.0）与租户配额。

## 类型化输入 / 输出
输入：
- `node`（string，必填）；
- `tier`（cloud/edge/home/cpu）、`role`（prefill/decode/cpu）；
- `mem_limit_gb`、`compute_pct`、`vllm_port`、`gpu_name`；
- `ctrl/token`；
- `dry-run`（bool）：只生成注册载荷不实际写入（离线/评测用）。

输出（JSON）：
- `onboarded`（bool）；
- `node`（string）；
- `profile`（object）：注册画像；
- `initial`（object）：`{reputation:1.0,status:"pending_first_heartbeat",quota}`；
- `health`（object）：探活结果；
- `next_steps`（array<string>）。

## 如何运行
```bash
python tool.py --node edge-2 --tier edge --role decode \
  --mem-limit-gb 8 --vllm-port 8104 --ctrl http://127.0.0.1:9000
```

## 确定性规则
- 探活失败 → 不注册，返回 health 失败与排查建议；
- 注册成功 → 初始信誉 1.0，等待首次心跳后转 online；
- 重名节点重新注册 → 覆盖画像（幂等）。

## 边界
只负责纳管与初始化；切分交由 gpu-slicer，调度交由 smart-dispatcher。
