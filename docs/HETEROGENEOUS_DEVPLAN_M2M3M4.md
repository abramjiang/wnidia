# WNIDIA 异构编排 · 完整开发文档（含 M2 / M3 / M4 开发内容）

> **范围**：仅 NVIDIA 全系列异构（不含国产卡；Apple 方向已搁置）
> **状态**：完整开发规格文档。配套可运行实现见 `heterogeneous/`（模块 + 试点脚本）。
>
> 配套文档：
> - `docs/HETEROGENEOUS_ARCHITECTURE.md`（总纲）
> - `docs/HETEROGENEOUS_UPGRADES.md`（①②③ 功能文档）
> - `docs/HETEROGENEOUS_USAGE.md`（应用说明）
> - `docs/HETEROGENEOUS_CHECKLIST.md`（接入与测试清单）
> - `docs/HETEROGENEOUS_PILOT_5090_GB10.md`（**首个试点案例**）
> - `docs/STRATEGY_POSITIONING.md`（商业定位）

---

## 硬件画像参考（各系列参数）

> ⚠️ 以下为**参考值，须按官方 datasheet 复核**；不同资料来源口径可能不同。

| 系列 | 代表型号 | 容量 | 带宽 | 精度支持 | 互联 |
|---|---|---|---|---|---|
| 数据中心 | H100 / H200 / B200 | 80–180GB | 数 TB/s | FP8 / FP4 | **NVLink** |
| 数据中心（上代） | A100 | 40 / 80GB | ≈2039 GB/s | FP16 / INT8 | NVLink |
| 专业工作站 | RTX 6000 Ada | 48GB (ECC) | ≈960 GB/s | FP8 / INT8 | 可选 NVLink |
| 消费级 | **RTX 5090** | 32GB GDDR7 | ≈1792 GB/s | FP4 / FP8 | 无 |
| 消费级 | RTX 4090 | 24GB | ≈1008 GB/s | FP8 / INT8 | 无 |
| 边缘 | Jetson Orin | 32–64GB 统一内存 | 低 | INT8 / FP16 | 无 |
| 一体机 | **GB10** | **128GB 统一内存** | ≈273 GB/s | FP4 / FP8 | 无 |

**关键洞察**：
- **容量与带宽往往不可兼得**——GB10 容量最大但带宽最低；5090 带宽最高但仅 32GB
- **消费级无 NVLink** → 跨卡 TP 受 PCIe 限制，宜用独立实例
- **精度支持随架构变化**：Blackwell 有 FP4，Ada 无

---

# 第一部分 · 六项应用场景：代码现状与补足清单

> 现状判定基于对 r9 源码的**实读**（文件 + 函数/字段级证据），非推测。

## 总览

| # | 场景 | 判定 | 一句话 |
|---|---|---|---|
| 1 | 混合负载（大模型 + 高并发） | ⚠️ 部分 | 能按容量/负载/时延路由，**缺并发量与阶段特征** |
| 2 | 跨代际混部（A100/H100/4090/5090） | ✅ 已实现 | generation + cc_level + CC_RANK + gates 齐全 |
| 3 | SLO 分层 | ✅ 已实现 | 时延预算硬约束 + 两套 tier 权重 |
| 4 | 成本优化与选型 | ⚠️ 部分 | 计量两层分离，但**无按 family 成本系数** |
| 5 | 边缘自治与降级 | ✅ 已实现 | offline.py + agent + 断网自治/恢复回放 |
| 6 | 能效优先 | ⚠️ 部分 | `power_w` 已采集，**未进入路由与计价** |

---

## 场景 1 · 混合负载（大模型 + 高并发并存）

**现状证据**
- `scheduler.py:83/111`：`n.free_mem_gb + 0.05 < t.need_mem_gb` → 容量硬过滤已实现
- `scheduler.score()`：已综合 `role / util / free_mem_gb / kv_hit / reputation / latency_ms / stability / uptime_ratio / cc_level / tier`
- **缺失**：无 `concurrency`（并发量）、无 `phase`（prefill/decode 阶段）、无 `model_size_gb`

**补足清单**
| 缺口 | 补足内容 | 对应模块 |
|---|---|---|
| 并发量 | `TaskFeature.concurrency` | 升级 ② |
| 阶段特征 | `TaskFeature.phase` + `bottleneck_of()` | 升级 ② |
| 模型规模 | `TaskFeature.model_size_gb` | 升级 ② |
| 瓶颈打分 | `profile_bonus()` 叠加进 `score()` | 升级 ②（M2 接入） |

---

## 场景 2 · 跨代际异构混部 ✅

**现状证据**
- `NodeProfile.generation`（代际标签）
- `NodeProfile.cc_level` + `capabilities.CC_RANK`
- `capabilities.py` 含 `resource.cross_generation`
- `gates.py` 门槛校验
- 三层 `layer`：center / edge / end

**补足清单**：基本完备。仅需把 `generation` 映射到 `DeviceProfile.arch`（blackwell / ada / hopper / ampere），供精度推导使用。

---

## 场景 3 · SLO 分层 ✅

**现状证据**
- `scheduler.py:50-62`：`latency_budget_ms > 0` 时作为**硬约束**
- `score()` 中两套 tier 权重：
  - 时延优先：`{'cloud':14,'edge':6,'home':0,'cpu':-6}`
  - 成本优先：`{'cloud':-4,'edge':8,'home':12,'cpu':10}`

**补足清单**：基本完备。可把"两套权重"显式化为配置项（当前为硬编码字典），便于按 SLO 档位切换。

---

## 场景 4 · 成本优化与选型

**现状证据**
- `metering.py`：`meter_*`（采数）/ `price_*`（算钱）**两层分离**
- 三种口径：token / node / seat，单价可配（`WNIDIA_PRICE_TOKEN` 等）
- `meter_from_usage()`：优先真实 usage，兜底估算并标记 `estimated`

**补足清单**
| 缺口 | 补足内容 | 对应模块 |
|---|---|---|
| 按 family 成本系数 | `COST_PROFILE` | 升级 ③ |
| 能耗计入 | `energy_cost(power_w, hours, 电价)` | 升级 ③ |
| 跨设备对比 | `compare()` | 升级 ③ |
| 成本进入路由 | 同等条件下选低成本节点 | M2 接入 |

---

## 场景 5 · 边缘自治与降级 ✅

**现状证据**
- `controller/offline.py`（`simulate_offline`）
- `agent/app.py`、`agent/agent_policy.py`
- `demo.py:166` 调用 `offline.simulate_offline('edge-1', seconds=120, tasks=3, ...)`
- demo 场景含"中心云中断承接 / 断网本地自治 / 恢复计量回放"

**补足清单**：基本完备。可把 DeviceProfile 的 `tags: ['edge']` 用于边缘优先路由。

---

## 场景 6 · 能效优先

**现状证据**
- `NodeProfile.power_w` 已定义并入库（`db.py:39/158/275`）
- **但**：grep 显示 `power_w` 未出现在 `scheduler` 或 `metering` 的计分/计价逻辑中

**补足清单**
| 缺口 | 补足内容 | 对应模块 |
|---|---|---|
| 能耗进计量 | `energy_cost()` | 升级 ③ |
| 能效进路由 | 低负载长驻任务偏好低功耗节点 | M2 接入（可选加权） |

---

# 第二部分 · 六项需求方：Harness 自动执行方案

> Harness 现状：`controller/harness.py` 已实现后台闭环
> （`detect_lost` / `launch_queued` / `reclaim_stuck_binding` / `poll_running` /
> `_verify_majority` / `_complete` / `inject_preemption` / `_tick`），
> 即"感知—匹配—执行—回流"完整链路，且已接入 `metering.record()` 与审计哈希链。

## 通用自动执行流水线（六方共用）

```
[入队 db.queued_tasks_ordered()]
   ↓
① 画像感知：DeviceProfile（容量/带宽/算力/精度/互联/功耗）
   ↓
② 特征识别：TaskFeature（规模/上下文/并发/阶段/隐私/精度/密级）
   ↓
③ 硬约束过滤（复用 scheduler）：容量 / 时延预算 / 密级 / 隐私
   ↓
④ 瓶颈打分（profile_bonus 叠加）：容量 or 带宽 or 算力
   ↓
⑤ 执行（executor）
   ↓
⑥ 计量回流：按画像成本系数 + 能耗 → metering.record()
   ↓
⑦ 审计哈希链（沿用现有）
```

---

## 需求方 1 · 中大型企业 IT/AI 平台

- **痛点**：私有化大模型，并发一上来就崩
- **自动策略**：并发 ≥ 阈值 → 带宽优先路由；大模型 → 容量优先
- **触发**：`launch_queued()` 自动出队，无需人工
- **验收**：并发下 P95 不劣化；单 token 成本可量化

## 需求方 2 · 行业 SI（医疗 / 政务 / 金融）

- **痛点**：硬件预算被压
- **自动策略**：成本优先权重 + `compare()` 选最低成本设备
- **验收**：同 SLO 下总成本下降可量化

## 需求方 3 · 城市算力中心 / 政务云

- **痛点**：多厂商设备无法统一调度计费
- **自动策略**：统一 DeviceProfile 注册 + 统一计量口径
- **验收**：异构设备同一账单、可审计

## 需求方 4 · AI 应用创业公司

- **痛点**：API 账单吃毛利
- **自动策略**：自建推理，按 token 口径 + family 系数计量
- **验收**：成本与公有云 API 对比可量化

## 需求方 5 · 运营商 / 边缘云

- **痛点**：回源带宽与中心压力
- **自动策略**：`tags:['edge']` 边缘优先承接；中心兜底
- **验收**：回源次数下降；边缘承接比例可统计

## 需求方 6 · 高校 / 科研平台

- **痛点**：多课题组共享算力无配额
- **自动策略**：按画像配额 + 计量计费
- **验收**：配额执行准确、计费可对账

---

# 第三部分 · M2 开发内容：接入（逐文件改动点）

> M2 = 把已实现的 ①②③ 模块**接入主链路**。以下为改动规格（非实现）。

## M2-1 路由接入

**文件**：`controller/scheduler.py` → `score()`

```python
# 在 score() 末尾追加（不改动既有任何一行）：
from .profile_routing import profile_bonus, infer_feature
from .device_profile import profile_of   # 需新增：node_id → DeviceProfile

feature = infer_feature(t.__dict__ if hasattr(t, '__dict__') else {})
s += profile_bonus(feature, profile_of(n))
```

**约束**：
- 硬约束（容量/时延/密级）**完全复用**，不重写
- `profile_bonus` 权重默认保守，通过环境变量可调
- `profile_of()` 未注册画像时返回 `None` → 该项记 0（**降级安全**）

## M2-2 计量接入

**文件**：`controller/metering.py` → `price()`

```python
def price(mode, *, tokens=0, node_seconds=0.0, seats=0, profile=None):
    # 现有三口径逻辑保持不变
    # 新增：乘以 cost_factor(profile)，并叠加 energy_cost(profile, hours)
```

**约束**：三种原口径（token/node/seat）行为不变；`profile=None` 时等价于现状。

## M2-3 画像注册

**文件**：`worker/agent.py`（或新增注册入口）

- worker 启动时上报 `DeviceProfile`（容量/带宽/算力/精度/互联/功耗/family/arch）
- 控制面注册进 `DeviceRegistry`
- **未上报**的节点画像为空 → 路由按现有逻辑（降级安全）

---

# 第四部分 · M3 开发内容：A/B 验证（量化收益）

> **M3 是把叙事变成事实的关键**——拿不出"路由前后差多少"，异构编排就只是 PPT。

## M3-1 验证目标

| 指标 | 定义 |
|---|---|
| P50 / P95 时延 | 任务完成时延分布 |
| 吞吐 | 单位时间完成任务数 |
| 单 token 成本 | 由计量层输出 |
| 路由命中率 | 任务是否被分到"瓶颈最匹配"的设备 |

## M3-2 验证方法

```
对照组 A：现有 scheduler.score()（不加 profile_bonus）
实验组 B：叠加 profile_bonus
同一负载分别跑 N 次 → 对比 P50/P95/吞吐/成本
```

**关键**：两组必须跑**同一负载**，且样本量足够（建议 ≥ 20 次）。

## M3-3 模块设计（规格）

```python
# controller/profile_ab.py（待实现）
def run_ab(registry, tasks, rounds=20) -> dict:
    """返回：
       {
         'A': {'p50':..,'p95':..,'throughput':..,'cost_cny':..},
         'B': {'p50':..,'p95':..,'throughput':..,'cost_cny':..},
         'delta_p95_pct': ..,
         'delta_cost_pct': ..,
         'hit_rate_B': ..
       }
    """
```

## M3-4 验收标准

- `delta_p95_pct` 与 `delta_cost_pct` **必须给出数值**（正或负都要如实报告）
- 若 B 不优于 A → **如实记录并调权重**，不得掩盖

---

# 第五部分 · M4 开发内容：Prefill / Decode 分离

> M4 是增强项，也是异构编排的最高价值形态。

## M4-1 原理

- **Prefill**：算力密集 → 调度到高算力设备
- **Decode**：带宽密集 → 调度到高带宽设备
- **KV cache**：容量密集 → 可放高容量设备

## M4-2 与现有 role 的关系（重要）

现有 `NodeProfile.role` 已有 `prefill / decode / cpu`，
且 `score()` 中已有 `s += 22 if n.role == want_role else 4`。

**M4 不是从零建**——现有已有 role 匹配机制，M4 要做的是：
1. 把 role 匹配从"静态标签"升级为"按任务阶段动态判定"
2. `want_role_for(t)` 结合 `TaskFeature.phase` 得出目标 role

## M4-3 模块设计（规格）

```python
# controller/phase_split.py（待实现）
def plan(t: TaskFeature, reg: DeviceRegistry) -> dict:
    """返回分阶段计划：
       {
         'prefill': {'node':..,'reason':..},
         'decode' : {'node':..,'reason':..},
         'kv_store': {'node':..},
         'split': True/False      # 是否值得分离（不值得则合并）
       }
    """
```

**约束**：
- 分离需评估跨设备传输开销；开销大于收益则**不分离**（`split=False`）
- 仅实例级分离，不做算子级异构并行（避免木桶效应）

---

# 第六部分 · 测试用例集（条件具备时一并执行）

| 编号 | 用例 | 预期 |
|---|---|---|
| T1 | 大模型任务 → 高容量设备 | 路由正确 |
| T2 | 高并发 decode → 高带宽设备 | 路由正确 |
| T3 | prefill-heavy → 高算力设备 | 路由正确 |
| T4 | 容量不足 → 拒绝并给原因 | 不崩溃、有 reason |
| T5 | 精度不匹配 → 降权或过滤 | 行为符合预期 |
| T6 | 计量：同任务不同设备成本可比 | 数值合理 |
| T7 | 计量：能耗项可开可关 | 开关生效 |
| T8 | A/B：B 组 P95 与成本 | **给出数值**（正负均如实） |
| T9 | 边缘自治：中心不可达 | 边缘承接，恢复后回放 |
| T10 | 降级安全：画像缺失 | 回落现有逻辑，不崩溃 |

---

# 第七部分 · 诚实边界（务必守住）

1. **成本系数为占位值**，须真实采购/折旧/电价替换后方可对外
2. **②③ 尚未接入主链路**——M2 完成前不得表述为"已生效"
3. **无 A/B 实测数据**——M3 完成前，异构收益未经验证
4. M4 分离需评估传输开销，不得无条件宣称"分离一定更快"
5. 硬件参数需按官方 datasheet 复核
6. 本文档为**开发规格**，实现与测试待条件具备后统一执行
