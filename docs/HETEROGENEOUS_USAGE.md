# 异构编排模块 · 应用说明

> **适用**：NVIDIA 全系列异构（数据中心 / 专业工作站 / 消费级 / 边缘 / 一体机）
> **状态**：下一版开发成果，**不属于参赛提交版本**；模块尚未接入主链路（M2 待实测后接入）。
> 配套：`docs/HETEROGENEOUS_ARCHITECTURE.md`（总纲）、`docs/HETEROGENEOUS_UPGRADES.md`（功能文档）、
> `docs/HETEROGENEOUS_DEVPLAN_M2M3M4.md`（完整开发规格）

---

## 1. 模块清单

| 模块 | 用途 | 自检 |
|---|---|---|
| `device_profile.py` | 设备画像抽象 + 注册表 | 14/14 |
| `profile_routing.py` | 任务特征 + 瓶颈路由 | 10/10 |
| `profile_metering.py` | 按画像成本计量 | 12/12 |
| `profile_ab.py` | A/B 路由收益验证（M3） | 8/8 |
| `phase_split.py` | Prefill/Decode 分离规划（M4） | 9/9 |

---

## 2. 部署

```bash
# 把模块放入工程的 controller/ 目录
cp heterogeneous/*.py <工程>/controller/

# 运行自检（无需 GPU，纯逻辑校验）
python3 controller/device_profile.py    # 14 项
python3 controller/profile_routing.py   # 10 项
python3 controller/profile_metering.py  # 12 项
python3 controller/profile_ab.py        # 8 项
python3 controller/phase_split.py       # 9 项
```

> ⚠️ 部署后**不会自动生效**——需完成 M2 接入（见第 7 节）。

---

## 3. 注册设备画像（NVIDIA 各系列示例）

```python
from controller.device_profile import DeviceProfile, DeviceRegistry

reg = DeviceRegistry()

# 数据中心
reg.register(DeviceProfile(
    node_id='h100-0', gpu_name='H100', family='datacenter', arch='hopper',
    capacity_gb=80, memory_model='discrete', bandwidth_gb_s=3350,
    compute_tflops={'fp8': 1979, 'fp16': 989},
    precision_support=['fp8', 'int8', 'fp16'],
    interconnect='nvlink', power_w=700, engine_pref=['vllm', 'trtllm']))

# 消费级（无 NVLink）
reg.register(DeviceProfile(
    node_id='5090-0', gpu_name='RTX-5090', family='consumer', arch='blackwell',
    capacity_gb=32, memory_model='discrete', bandwidth_gb_s=1792,
    compute_tflops={'fp4': 2000, 'fp8': 900},
    precision_support=['fp4', 'fp8', 'int8', 'fp16'],
    interconnect='none', power_w=575, engine_pref=['vllm', 'ollama']))

# 一体机（统一内存）
reg.register(DeviceProfile(
    node_id='gb10-0', gpu_name='GB10', vendor='nvidia', family='soc', arch='blackwell',
    capacity_gb=128, memory_model='unified', bandwidth_gb_s=273,
    compute_tflops={'fp8': 300},
    precision_support=['fp4', 'fp8'],
    interconnect='none', power_w=240, engine_pref=['ollama']))

# AMD（多厂商）
reg.register(DeviceProfile(
    node_id='mi300-0', gpu_name='AMD Instinct MI300X', vendor='amd',
    family='datacenter', arch='cdna3', capacity_gb=192, memory_model='discrete',
    bandwidth_gb_s=5300, compute_tflops={'fp8': 1300},
    precision_support=['fp8', 'int8', 'fp16'],
    interconnect='pcie', power_w=750, engine_pref=['vllm']))

# Intel（多厂商）
reg.register(DeviceProfile(
    node_id='gaudi-0', gpu_name='Intel Gaudi 3', vendor='intel',
    family='datacenter', arch='gaudi', capacity_gb=128, memory_model='discrete',
    bandwidth_gb_s=3600, compute_tflops={'fp8': 900},
    precision_support=['fp8', 'int8', 'fp16'],
    interconnect='pcie', power_w=600, engine_pref=['vllm']))

> **多厂商字段**：`vendor`（nvidia / amd / intel / other）为本轮新增，
> 与 `interconnect`（nvlink / ualink / cxl / ucie / pcie / none）共同支撑跨厂商画像。

# 边缘
reg.register(DeviceProfile(
    node_id='jetson-0', gpu_name='Jetson-Orin', family='edge', arch='ampere',
    capacity_gb=32, memory_model='unified', bandwidth_gb_s=204,
    compute_tflops={'int8': 130}, precision_support=['int8', 'fp16'],
    interconnect='none', power_w=60, engine_pref=['llama.cpp'],
    tags=['edge']))
```

**参数核对**：`bandwidth_gb_s` / `compute_tflops` / `power_w` 请按官方 datasheet 填写；
本文数值为示例，**不保证与实际规格一致**。

---

## 4. 画像路由

```python
from controller.profile_routing import TaskFeature, route, infer_feature

# 高并发 decode
r = route(TaskFeature(model_size_gb=8, concurrency=32, phase='decode-heavy'), reg)
print(r['node'], r['reason'])

# 大模型（容量优先）
r = route(TaskFeature(model_size_gb=60, context_tokens=2000), reg)

# 从任务字典推断特征
f = infer_feature({'need_mem_gb': 12, 'concurrency': 4, 'latency_budget_ms': 300})
```

路由结果含 `reason`，可直接用于界面展示与审计。

---

## 5. A/B 验证（M3）

```python
from controller.profile_ab import run_ab

tasks = [
    TaskFeature(model_size_gb=8,  concurrency=32, phase='decode-heavy'),
    TaskFeature(model_size_gb=60, context_tokens=2000),
    TaskFeature(model_size_gb=8,  phase='prefill-heavy', precision_pref='fp8'),
]
r = run_ab(reg, tasks)
print('基线命中率 %.0f%% → 画像路由 %.0f%%' % (r.a_hit_rate*100, r.b_hit_rate*100))
print('成本变化 %.2f%%' % r.delta_cost_pct)
```

⚠️ **本模块只评估"路由决策质量"与"估算成本"；
真实 P50/P95 时延必须真实执行任务才能测得。**

---

## 6. Prefill / Decode 分离（M4）

```python
from controller.phase_split import plan

p = plan(TaskFeature(model_size_gb=8, concurrency=64,
                     context_tokens=2000, phase='decode-heavy'), reg)
print(p.split, p.prefill_node, '->', p.decode_node, '|', p.reason)
```

模块会估算 KV 传输开销；**不划算时返回 `split=False`（合并执行）**，不会无条件分离。

---

## 7. M2 接入（待实测后执行，当前未做）

改动极小，共两处：

```python
# ① controller/scheduler.py 的 score() 末尾追加（不改既有任何一行）
from .profile_routing import profile_bonus, infer_feature
s += profile_bonus(feature, profile_of(n))

# ② controller/metering.py 的 price() 增加 profile 参数
def price(mode, *, tokens=0, node_seconds=0.0, seats=0, profile=None):
    # 原三口径逻辑不变；额外乘 cost_factor(profile) 并叠加 energy_cost()
```

**硬约束（容量/时延/密级）完全复用，不重写。** 未注册画像时该项记 0（降级安全）。

---

## 8. 六项需求方应用剧本

| 需求方 | 用法 |
|---|---|
| 中大型企业 IT 平台 | 并发任务用 `concurrency` + `phase='decode-heavy'` 路由到高带宽卡；大模型用 `model_size_gb` 路由到高容量卡 |
| 行业 SI | `run_ab()` 对比不同硬件组合成本，支撑选型 |
| 城市算力中心 / 政务云 | 统一注册异构卡画像 + `profile_metering` 统一计量口径 |
| AI 应用创业公司 | `profile_metering.compare()` 对比自建 vs 云 API 成本 |
| 运营商 / 边缘云 | `tags=['edge']` 边缘优先承接；中心兜底 |
| 高校 / 科研 | 按画像配额 + `profile_metering` 计费 |

---

## 9. 测试用例集（待实测条件具备后统一执行）

| 编号 | 用例 | 预期 |
|---|---|---|
| T1 | 大模型 → 高容量卡 | 路由正确 |
| T2 | 高并发 decode → 高带宽卡 | 路由正确 |
| T3 | prefill-heavy → 高算力卡 | 路由正确 |
| T4 | 容量不足 | 拒绝并给 reason，不崩 |
| T5 | 精度不匹配 | 降权或过滤 |
| T6 | 计量跨设备可比 | 数值合理 |
| T7 | 能耗项开关 | 生效 |
| T8 | A/B P95 与成本 | **给出数值**（正负如实） |
| T9 | 边缘自治 | 边缘承接，恢复回放 |
| T10 | 画像缺失 | 回落现有逻辑，不崩 |

---

## 10. 诚实边界

- **成本系数为占位值**，须真实采购/折旧/电价替换后方可对外
- 模块**尚未接入主链路**，不得表述为"已生效"
- **无 A/B 实测数据**；M3 当前只测决策质量，不测真实时延
- M4 传输模型为简化参数，需实测校准
- 硬件参数需按官方 datasheet 复核
