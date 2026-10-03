# heterogeneous · 异构编排（下一版开发成果）

> **重要说明：本目录为「下一版开发成果」，不属于参赛提交版本的源码。**
>
> - 仓库 `src/` 为 **r9 提交快照**（已脱敏），与参赛提交物严格一致，**保持冻结**。
> - 本目录内容用于在后续版本中把 WNIDIA 扩展为**跨架构中立编排**，
>   目前**尚未接入调度逻辑**，请勿表述为"已生效"。

---

## 目录内容

| 文件 | 说明 | 自检 |
|---|---|---|
| `device_profile.py` | 设备画像抽象（①）—— 数据结构 + 注册表 | 14/14 |
| `profile_routing.py` | 任务特征 + 瓶颈路由（②）—— 叠加式，不改现有调度 | 10/10 |
| `profile_metering.py` | 按画像成本计量（③）—— 统一跨架构口径 | 12/12 |
| `README.md` | 本文件 | — |

运行自检：`python3 controller/<模块名>.py`

配套文档：
- `docs/HETEROGENEOUS_ARCHITECTURE.md` —— 架构总纲
- `docs/HETEROGENEOUS_UPGRADES.md` —— **开发与功能文档（含可植入需求与应用场景）**

---

## 使用方式

本文件在工程中应放置于 `controller/` 目录下，例如：

```bash
cp heterogeneous/device_profile.py <工程>/controller/
```

```python
from controller.device_profile import DeviceProfile, DeviceRegistry

reg = DeviceRegistry()
reg.register(DeviceProfile(
    node_id='h100-0', gpu_name='H100', family='datacenter', arch='hopper',
    capacity_gb=80, memory_model='discrete', bandwidth_gb_s=3350,
    compute_tflops={'fp8': 1979, 'fp16': 989},
    precision_support=['fp8', 'int8', 'fp16'], interconnect='nvlink',
    power_w=700, engine_pref=['vllm', 'trtllm'],
))
reg.register(DeviceProfile(
    node_id='mac-01', gpu_name='Apple-M-Ultra', family='apple-silicon', arch='apple',
    capacity_gb=128, memory_model='unified', bandwidth_gb_s=800,
    compute_tflops={'fp16': 100},
    precision_support=['fp16', 'int8'], interconnect='none',
    power_w=200, engine_pref=['mlx', 'llama.cpp'],
    tags=['local', 'privacy-capable'],
))

reg.best_for_capacity(100)   # → mac-01（128GB 统一内存）
reg.best_for_bandwidth()     # → h100-0（3350 GB/s）
reg.best_for_compute('fp8')  # → h100-0
reg.by_tag('privacy-capable')  # → [mac-01]
```

运行内置自检：

```bash
python3 controller/device_profile.py
# 14 项断言全部 PASS
```

---

## 设计要点

1. **不替换** 现有 `models.NodeProfile`，仅作补充结构 → 渐进接入，不破坏现有调度
2. **新增硬件只注册画像，不改调度逻辑** → 纳管 ≠ 适配
3. **一张表容纳 CUDA 与 Apple Silicon** → 差异只在字段值，这是"中立编排"成立的技术前提

### ⚠️ 与现有字段的重要区分

`NodeProfile.bandwidth_mbps` 是**上行网络带宽 (Mbps)**；
本结构的 `bandwidth_gb_s` 是**显存带宽 (GB/s)**，是异构路由的核心维度。
**二者不可混淆。**

---

## 当前状态

- ✅ 已实现并通过内置自检（14/14）
- ❌ **尚未接入调度器**（升级方案 ② 才消费画像）
- ❌ 尚未接入计量（升级方案 ③）

请勿在对外材料中表述为"已生效"或"已上线"。
