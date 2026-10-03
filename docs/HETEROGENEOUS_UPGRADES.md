# WNIDIA 异构编排升级 · 开发与功能文档

> 覆盖三项升级：① DeviceProfile ② 画像路由 ③ 按画像计量
> 模块位于 `heterogeneous/`，**部署时放入 `controller/` 目录**。
> **状态：下一版开发成果，不属于参赛提交版本；目前尚未接入调度与计量主链路。**

---

## 0. 总览

| 模块 | 功能 | 自检 | 接入状态 |
|---|---|---|---|
| `device_profile.py` | 设备画像抽象 + 注册表 | 14/14 PASS | 地基（② ③ 依赖） |
| `profile_routing.py` | 任务特征 + 瓶颈路由 | 10/10 PASS | 设计叠加，未接入 `scheduler` |
| `profile_metering.py` | 按画像成本计量 | 12/12 PASS | 未接入 `metering` 主链路 |

运行自检：

```bash
python3 controller/device_profile.py     # 14 项
python3 controller/profile_routing.py    # 10 项
python3 controller/profile_metering.py   # 12 项
```

---

## 1. 模块一：DeviceProfile（设备画像）

### 功能
- 统一描述 CUDA 与 Apple Silicon 设备，**差异只体现在字段值上**
- 受控词表校验（family / memory_model / interconnect / precision）
- 按瓶颈选设备：容量 / 带宽 / 算力

### 主要 API

```python
DeviceProfile(node_id, gpu_name, family, arch, capacity_gb,
              memory_model, bandwidth_gb_s, compute_tflops,
              precision_support, interconnect, power_w, engine_pref, tags)

.validate() / .is_valid()
.supports(precision) / .compute_of(precision) / .has_tag(tag)
.to_dict() / DeviceProfile.from_dict()

DeviceRegistry().register(p) / .get() / .all()
.by_family(family) / .by_tag(tag)
.best_for_capacity(need_gb) / .best_for_bandwidth() / .best_for_compute(precision)
```

### ⚠️ 与现有字段的关键区分
`NodeProfile.bandwidth_mbps` 是**上行网络带宽 (Mbps)**；
`DeviceProfile.bandwidth_gb_s` 是**显存带宽 (GB/s)**，是异构路由核心维度。**不可混淆。**

### 植入点
`controller/models.py` 的 `NodeProfile` 保持不动；
新增画像由 worker/agent 上报后注册进 `DeviceRegistry`。

---

## 2. 模块二：画像路由

### 功能
- `TaskFeature`：补上"任务是什么"（模型规模/上下文/并发/阶段/隐私/精度/密级）
- `bottleneck_of()`：判定瓶颈（容量 / 带宽 / 算力）
- `profile_bonus()`：瓶颈匹配加分，**叠加**到现有 `scheduler.score()`
- `route()`：硬约束过滤 + 瓶颈选优，并返回**选择理由**（便于演示与审计）

### 本质变化
- 现有：回答"**哪个节点现在有空**"
- 升级：回答"**哪个节点最适合这类任务**"

### 植入方式（叠加式，不推翻）

```python
# 在 controller/scheduler.py 的 score() 末尾加一项：
from controller.profile_routing import profile_bonus, infer_feature

feature = infer_feature(task_dict)
s += profile_bonus(feature, profile_of(n))     # 权重可配，默认保守
```

**硬约束（容量、时延预算、密级）完全复用现有实现，不重写**——
`route()` 只额外处理隐私与精度过滤。

### 路由行为示例（自检已验证）
| 任务 | 选中 | 原因 |
|---|---|---|
| 大模型 100GB | Mac(128GB) | 唯一满足容量 |
| 高并发 decode | H100 | 带宽最高 |
| prefill-heavy + fp8 | H100 | 算力与精度均命中 |
| 隐私任务 | Mac | 强制留在本地 |
| 容量 500GB | 拒绝 | 无设备满足，给出原因 |

---

## 3. 模块三：按画像计量

### 功能
- 在现有 `metering.py`（meter_*/price_* 两层分离）之上加**设备成本系数**与**能耗项**
- **跨架构统一口径**：Mac 与 NVIDIA 不比算力，只比"完成的同类任务量"
- `compare()`：多设备成本对比，回答"在哪跑更划算"

### 计价公式
```
金额 = 基础度量 × 设备成本系数 + 能耗成本
基础度量 = tokens（推荐，跨架构可比）或 node_hours
能耗成本 = 功率(kW) × 小时 × 电价
```

### 成本系数（**占位，需真实数据替换**）
| family | 系数 |
|---|---|
| datacenter | 1.00（基准） |
| workstation | 0.60 |
| consumer | 0.45 |
| edge | 0.20 |
| soc | 0.30 |
| apple-silicon | 0.25 |

未知 family 回落 1.0（保守，不低估）。

### 植入点
`controller/metering.py` 的 `price()` 增加 `profile` 参数；
调用处传入节点画像即可，原有三种口径（token/node/seat）保持不变。

---

## 4. 可植入的需求（需求方 → 痛点 → 对应模块）

| 需求方 | 痛点 | 植入什么 | 满足方式 |
|---|---|---|---|
| **中大型企业 IT/AI 平台** | 私有化大模型并发一上来就崩 | ② 画像路由 | 按瓶颈把任务分到最合适的卡，控单 token 成本 |
| **行业 SI（医疗/政务/金融）** | 硬件预算被压 | ② + ③ | 用更便宜的硬件组合达成 SLO，并量化成本 |
| **城市算力中心 / 政务云** | 多厂商设备无法统一调度计费 | ① + ③ | 统一画像 + 统一计量口径 |
| **AI 应用创业公司** | API 账单吃掉毛利 | ③ | 自建推理的成本可比、可优化 |
| **运营商 / 边缘云** | 回源带宽与中心压力 | ②（tags: edge） | 边缘承接、中心兜底 |
| **高校 / 科研** | 多课题组共享算力无配额 | ① + ③ | 按画像配额与计费 |

---

## 5. 应用场景

### 场景 A：混合负载（大模型 + 高并发并存）
- 大模型/长上下文 → 高容量设备（GB10 / B200 / Mac 大内存）
- 高并发小模型 → 高带宽设备（H100 / 5090）
- **价值**：同构堆卡做不到两端兼顾

### 场景 B：Mac ↔ NVIDIA 混编（差异化楔子）
- 开发/调试/隐私任务 → Mac（本地优先）
- 超出容量或并发阈值 → 自动转 NVIDIA
- 统一计量 → 一张账单可比成本

### 场景 C：SLO 分层
- 关键低时延 → 高算力卡
- 批量长任务 → 大容量卡
- 路由理由可审计（`route()` 返回 `reason`）

### 场景 D：成本优化与选型
- `compare()` 对比同类任务在不同设备的成本
- 支撑"该买哪种卡"的采购决策

### 场景 E：边缘自治与降级
- 中心不可达 → 边缘设备（tags: edge）承接
- 与现有边缘自治能力衔接

### 场景 F：能效优先
- 低负载长驻任务 → 低功耗设备（Apple Silicon / edge）
- 计量中能耗项可单独关闭以做对比

---

## 6. 诚实边界（务必守住）

- **成本系数为占位设计**，必须由真实采购/折旧/电价数据替换后才能对外
- 新模块**尚未接入** `scheduler` / `metering` 主链路，不得表述为"已生效"
- 路由权重需实测调优（M2 单机实测后），初始值保守
- **尚无 A/B 实测数据**——M2 前，异构收益未经验证
- 硬件参数需按官方 datasheet 复核
- 计量目前无外部审计，称"可信计量"前需解决认证问题
