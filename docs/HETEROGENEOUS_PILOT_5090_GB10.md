# 首个试点：RTX 5090 + GB10 异构

> **定位**：N 卡全系列异构的**首个落地案例**。
> 通用方案见 `HETEROGENEOUS_DEVPLAN_M2M3M4.md`；本文讲"第一个怎么跑"。
> 可运行实现：`heterogeneous/pilot_5090_gb10.py`（自检 9/9）。

---

## 1. 为什么先做这一对

| 维度 | GB10 | RTX 5090 | 反差 |
|---|---|---|---|
| 容量 | **128 GB 统一内存** | 32 GB | **4×** |
| 带宽 | ≈273 GB/s | **≈1792 GB/s** | **6.5×** |
| 算力 | 中 | 高 | — |
| 功耗 | ≈240W | 575W | — |
| 架构 | Blackwell | Blackwell | 同代（FP4 对齐） |

**反差最大** → 最能验证"按瓶颈路由"的价值。
用具体 pair 跑出数字，再推广到 A100 / H100 / 4090 / Jetson 全系列。

---

## 2. 设备画像

```python
GB10 = dict(
    node_id='gb10-0', gpu_name='GB10', family='soc', arch='blackwell',
    capacity_gb=128, memory_model='unified', bandwidth_gb_s=273,
    compute_tflops={'fp8': 300, 'fp4': 600}, precision_support=['fp4', 'fp8'],
    interconnect='none', power_w=240, engine_pref=['ollama'])

RTX5090 = dict(
    node_id='5090-0', gpu_name='RTX-5090', family='consumer', arch='blackwell',
    capacity_gb=32, memory_model='discrete', bandwidth_gb_s=1792,
    compute_tflops={'fp8': 900, 'fp4': 2000}, precision_support=['fp4', 'fp8'],
    interconnect='none', power_w=575, engine_pref=['vllm', 'ollama'])
```

> 参数请按官方 datasheet 复核；本文数值为示例。

---

## 3. 运行

```bash
cp heterogeneous/*.py controller/
python3 controller/pilot_5090_gb10.py
```

输出：4 个路由场景 + 成本对比 + A/B 命中率 + 分离规划 + 完整 JSON 报告。

---

## 4. 试点结果（实测，非真机时延）

| 场景 | 期望 | 实得 | 结论 |
|---|---|---|---|
| 高并发 decode（小模型） | 5090 | **5090** ✅ | 带宽优先生效 |
| 大模型 100GB | GB10 | **GB10** ✅ | 只有 128GB 装得下 |
| prefill-heavy | 5090 | **5090** ✅ | 算力优先生效 |
| 长上下文 128k | GB10 | **GB10** ✅ | 容量优先生效 |
| 成本对比 | — | 最划算 **GB10** | 计量按画像差异化生效 |
| A/B 命中率 | — | 基线 **50%** → 画像路由 **100%** | +50pp |
| 分离规划 | — | split=False（同设备） | 有明确理由 |
| 容量 500GB | 拒绝 | 正确拒绝 ✅ | 不崩溃 |

---

## 5. 试点 → 推广路径

```
试点：5090 + GB10（跑通、出数字）
   ↓
验证：A/B 真实 P50/P95（需真机）
   ↓
推广：A100 / H100 / 4090 / 6000Ada / Jetson 全系列
   ↓
增强：Prefill/Decode 分离（评估传输开销后决定）
```

---

## 6. 已知易错点（开发中遇到）

**`profile_of()` 只查模块级 `DEFAULT_REGISTRY`**
若用自建 `DeviceRegistry()` 注册（如试点脚本），
`profile_of()` 会返回 None → 计量按未知设备处理（成本系数回落 1.0）。
**应使用自建实例的 `.get(node_id)`。**
（已在 `device_profile.py` docstring 中标注）

---

## 7. 诚实边界

- **A/B 的 50%→100% 是"路由决策命中率"**，不是真实性能提升
- 真实 P50/P95 需真机执行（当前无实测条件）
- 成本系数为占位值，须真实采购/折旧/电价替换
- 试点模块未接入主链路；接入步骤见 `HETEROGENEOUS_CHECKLIST.md`
