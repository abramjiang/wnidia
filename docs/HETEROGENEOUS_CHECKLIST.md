# 异构编排 · 接入与测试可执行 Checklist

> 配套：`docs/HETEROGENEOUS_USAGE.md`（应用说明）、`docs/HETEROGENEOUS_DEVPLAN_M2M3M4.md`（开发规格）
> 适用：NVIDIA 全系列异构。**src/ 保持冻结**，所有改动在副本或 `heterogeneous/` 内进行。

---

## 0. 前置条件

- [ ] 工程可运行（依赖已装：`pip install -r requirements.txt`）
- [ ] Python 3.9+、`tmux` 可用
- [ ] 已确认 `heterogeneous/` 五个模块已放入 `controller/`
- [ ] 备份：`cp -r controller controller.bak`（**必须，便于回滚**）

---

## 1. 部署模块（无需 GPU）

```bash
cp heterogeneous/*.py controller/
for m in device_profile profile_routing profile_metering profile_ab phase_split; do
  python3 controller/$m.py || echo "FAIL $m"
done
```

- [ ] `device_profile.py` → 14/14
- [ ] `profile_routing.py` → 10/10
- [ ] `profile_metering.py` → 12/10
- [ ] `profile_ab.py` → 8/8
- [ ] `phase_split.py` → 9/9

---

## 2. 注册设备画像

按 `docs/HETEROGENEOUS_USAGE.md` 第 3 节注册各节点画像
（H100 / RTX 5090 / GB10 / Jetson 示例已给出）。

- [ ] 每个节点均已 `register_profile()`
- [ ] 参数已按官方 datasheet 核对（**不照抄示例数值**）
- [ ] `memory_model` 正确（统一内存设备填 `unified`）
- [ ] `precision_support` 正确（Blackwell 含 fp4，老架构不含）

验证：
```python
from controller.device_profile import profile_of
print(profile_of('h100-0').bandwidth_gb_s)
```
- [ ] 能按 node_id 取到画像

---

## 3. M2 接入（两处，各一行级改动）

### 3.1 路由接入
在 `controller/scheduler.py` 的 `score()` 末尾追加：

```python
from .profile_routing import profile_bonus, infer_feature
from .device_profile import profile_of
s += profile_bonus(infer_feature(t.__dict__), profile_of(n.node))
```

- [ ] 追加完成，**未改动既有任何一行**
- [ ] 硬约束（容量/时延/密级）保持原样

### 3.2 计量接入
`controller/metering.py` 的 `price()` 增加 `profile=None` 参数：

```python
# 原三口径逻辑不变；额外乘 cost_factor(profile) 并叠加 energy_cost()
```

- [ ] `profile=None` 时行为与现状完全一致
- [ ] 调用处传入 `profile_of(node.node)`

---

## 4. 冒烟验证（无需 GPU）

- [ ] 启动服务（`MODE=mock bash scripts/deploy_gx10.sh`）
- [ ] `/healthz` 返回 ok
- [ ] 提交任务能正常派发
- [ ] 未注册画像的节点**仍能正常调度**（降级安全）

---

## 5. 功能测试 T1–T10

| # | 用例 | 通过标准 |
|---|---|---|
| T1 | 大模型 → 高容量卡 | 路由正确 |
| T2 | 高并发 decode → 高带宽卡 | 路由正确 |
| T3 | prefill-heavy → 高算力卡 | 路由正确 |
| T4 | 容量不足 | 返回 None + reason，不崩 |
| T5 | 精度不匹配 | 降权或过滤 |
| T6 | 计量跨设备可比 | 数值合理 |
| T7 | 能耗项开关 | 生效 |
| T8 | A/B P95 与成本 | **给出数值**（正负如实） |
| T9 | 边缘自治 | 边缘承接，恢复回放 |
| T10 | 画像缺失 | 回落现有逻辑，不崩 |

- [ ] T1–T10 全部执行并记录结果

---

## 6. A/B 验证（M3）

```python
from controller.profile_ab import run_ab
r = run_ab(reg, tasks); print(r.a_hit_rate, r.b_hit_rate, r.delta_cost_pct)
```

- [ ] 基线命中率与画像路由命中率均已记录
- [ ] 成本变化百分比已记录
- [ ] **若 B 不优于 A → 如实记录并调权重，不得掩盖**

---

## 7. 分离验证（M4）

```python
from controller.phase_split import plan
p = plan(TaskFeature(...), reg); print(p.split, p.reason)
```

- [ ] 分离/不分离决策均有明确 reason
- [ ] 长上下文场景传输开销被正确计算
- [ ] 不划算时 `split=False`（合并执行）

---

## 8. 真实时延测试（**需真机**，当前无法执行）

> ⚠️ M3 模块只测"路由决策质量"，**真实 P50/P95 必须真实执行**。

- [ ] 两种 NVIDIA 卡型可用
- [ ] 同一负载分别跑 A/B 两组，各 ≥ 20 次
- [ ] 记录真实 P50 / P95 / 吞吐
- [ ] 与估算成本交叉核对

---

## 9. 校准与收尾

- [ ] 成本系数替换为真实采购/折旧/电价数据
- [ ] M4 传输模型参数（`HIDDEN_BYTES_PER_TOKEN`、互联带宽表）实测校准
- [ ] 路由权重按实测调优
- [ ] 更新文档，移除"未验证"表述

---

## 10. 回滚方案

```bash
tmux kill-session -t wnidia
rm -rf controller && mv controller.bak controller
```

- [ ] 回滚后服务可正常启动

---

# 附：已实测跑通 vs 待真机验证

> 以下"已跑通"结论来自 **用真实 `scheduler`/`metering` 代码**执行的端到端验证
> （替换 `db.all_nodes()` 提供节点，其余硬约束、打分、排序逻辑全部走真实代码）。

## ✅ 已实测跑通（无需 GPU）

| 环节 | 实测结果 |
|---|---|
| 基础调度 | 正常派发（H100） |
| 大模型 100GB | 仅 GB10(128GB) 满足容量 ✅ |
| 高并发任务 | 正常调度 |
| 容量 999GB | 正确返回 None，**不崩溃** |
| 计量按画像差异化 | H100 **2.42** > GB10 **0.74** |
| 成本对比 | 最划算 = GB10 ✅ |
| A/B 验证 | 基线 **33%** → 画像路由 **100%** |
| 分离规划 | 可执行，prefill/decode 明确 |
| **降级安全** | 未接画像时调度仍正常 ✅ |

## ⏳ 待真机验证（当前无实测条件）

| 项 | 原因 |
|---|---|
| 真实 P50/P95 时延 | 需真实 GPU 推理执行 |
| 真实吞吐 | 同上 |
| A/B 的真实性能差 | M3 当前只测决策质量 |
| M4 传输开销校准 | 需真实互联带宽实测 |
| 成本系数 | 需真实采购/电价数据 |

## 结论

> **逻辑链路与降级路径已完整跑通；唯一未验证的是真实硬件上的时延与吞吐。**
> 因此对外**不得**宣称"性能提升 X%"——只能说"路由决策命中率从 33% 提升到 100%"（仿真）。
