# WNIDIA · P0 五项实现开发文档

> **本轮完成**：P0（F1–F5）全部实现，手工/硬件部分以**模拟**方式预备真实接入
> **状态**：代码已完成并自检通过；**尚未上传 GitHub**（待确认）
> 范围：仅美股大厂（NVIDIA / AMD / Intel），不含国产卡

---

## 一、本轮交付清单

| 模块 | 对应 | 功能 | 自检 |
|---|---|---|---|
| `device_discovery.py` | **F1** | 设备自动发现与画像注册 | 16/16 |
| `telemetry.py` | **F2** | 统一遥测适配层（厂商无关） | 8/8 |
| `compliance_policy.py` | **F3** | 合规策略可配置化 | 10/10 |
| `tenant_metering.py` | **F4** | 租户/项目维度计量 | 9/9 |
| `k8s_adapter.py` | **F5** | K8s 接入形态（模拟）+ Prometheus 导出 | 8/8 |
| `device_profile.py`（更新） | 结构补齐 | 新增 `vendor` 字段；`interconnect` 扩展 | 14/14 |
| `phase_split.py`（更新） | 一致性修复 | 互联带宽表补齐 | 10/10 |

**合计 11 个模块，全部自检通过**（含原有 6 个）。

---

## 二、各模块说明

### F1 · 设备自动发现（`device_discovery.py`）

- **真实探测**：`probe_nvidia()`（nvidia-smi）、`probe_amd()`（rocm-smi）、`probe_intel()`（hl-smi）
- **模拟**：`simulate_devices()` 产出 H100 / RTX 5090 / MI300X / Gaudi3 四设备
- **降级**：命令不存在时返回空列表，**不抛异常**
- **带宽处理**：`nvidia-smi` 不直接给出显存带宽 → 用 `SPEC_TABLE` 查表；查不到记 0，**不猜测**
- **精度推导**：按架构（Blackwell 含 FP4，Hopper/Ada 含 FP8，Ampere 只到 INT8/FP16）

```python
from controller.device_discovery import discover, register_discovered
profiles = discover(simulate=True)            # 模拟
n, errs  = register_discovered(reg, simulate=True)
```

### F2 · 统一遥测（`telemetry.py`）

- `UnifiedMetrics`：厂商无关的统一指标（util / mem / power / temp）
- 三个 provider + 模拟 provider；真实失败**自动降级为模拟**，上层不中断
- 意义：**中立性的技术基础**——上层只消费归一化指标，不碰厂商 SDK

### F3 · 合规策略可配置化（`compliance_policy.py`）

- 把硬编码的 `secret_rank → trusted / cc_level / 主权域` 外置为可配置策略
- `DEFAULT_POLICY` **等价于现有硬编码行为**（迁移不回退）
- 支持自定义（如金融行业要求 `finance-approved` 标签）

```python
custom = CompliancePolicy.from_dict({
    'name': 'finance',
    'rules': {'L2': {'require_trusted': True, 'require_tags': ['finance-approved']}}})
evaluate(node_ctx, {'secret_rank': 'L2'}, custom)
```

### F4 · 租户/项目计量（`tenant_metering.py`）

- 从 per-task 记录聚合到**租户 / 项目 / 节点**三维
- 输出：各租户-项目成本、各节点贡献、`estimated` 计数（真实 vs 估算）
- `tenant_report()` 出单租户账单

### F5 · K8s 接入（`k8s_adapter.py`，模拟）

- `parse_node_labels()`：从标签解析画像（真实环境由 NFD / GPU Operator 提供标签）
- `admit()`：结合合规策略做准入判断（模拟调度器扩展 / 准入 webhook）
- `prometheus_metrics()`：导出 Prometheus 文本格式（**顺带补足 F10**）
- 真实接入时把 `admit()` 接到 webhook、`prometheus_metrics()` 挂到 `/metrics`，**业务逻辑无需改动**

---

## 三、开发中发现并修复的 Bug（3 个，均为自检抓出）

### Bug 1：消费级 Blackwell 被误判为 NVLink
`interconnect` 原按架构判定 → RTX 5090（Blackwell 消费卡，**无 NVLink**）被误判为 `nvlink`。
**修复**：改为按形态判定（NVIDIA 数据中心卡才给 nvlink），并加断言防回归。

### Bug 2：Apple 关键字 `M3` 误匹配 "HBM3"
H100 的显存标识 **HBM3** 含子串 `M3` → H100 被误判为 Apple 统一内存设备。
**修复**：Apple 芯片改用**词边界正则** `\b(M1|M2|M3|M4)\b` 匹配。

### Bug 3：扩展 `interconnect` 后跨模块不一致（自己引入）
给 `DeviceProfile` 增加 `ualink/cxl/ucie` 后，`phase_split.py` 的带宽表未同步 → 新类型**静默回落到最慢的 `none`(8 GB/s)**。
**修复**：补齐带宽表（ualink 200 / cxl 64 / ucie 32 GB/s），并加**一致性断言**——自检中动态校验带宽表必须覆盖 `INTERCONNECTS` 全部取值。

> 这三个 bug 都是**自检抓出来的**，不是静态审查发现的——再次说明"跑一遍"的价值。

---

## 四、与现有 GitHub 内容的差异 / 待同步项

| # | 问题 | 说明 | 状态 |
|---|---|---|---|
| G1 | **README 模块清单过期** | GitHub `heterogeneous/README.md` 列 6 个模块，现为 11 个 | 待同步 |
| G2 | **`HETEROGENEOUS_USAGE.md` 未含新模块** | 应用说明缺 F1–F5 用法 | 待同步 |
| G3 | **DEVPLAN 硬件画像表未含 vendor** | 新增 `vendor` 维度未体现 | 待同步 |
| G4 | **新模块未上传** | 5 个新模块 + 2 个更新尚未推送 | 待确认 |
| G5 | `src/` 冻结无影响 | ✅ 本轮未改动 `src/` | 正常 |

**注意**：以上 G1–G3 属于**文档漂移**（代码已超前于文档），不是功能 bug。

---

## 五、验证结果

| 检查 | 结果 |
|---|---|
| 11 个模块自检 | **全部 ALL PASS** |
| 脱敏快检（新模块） | **0 命中** |
| e2e 工作流（真实 scheduler） | ✅ 完全跑通 |
| selfcheck L1+L2 | Python 语法 **75/75 PASS** |
| `compileall controller/` | OK |
| `src/` | **未改动，仍冻结 135** |

唯一 FAIL 是 selfcheck 的"脱敏无泄漏"——**预期**，因工作树本身含真实 IP（非新模块问题）。

---

## 六、诚实边界

1. F1/F2 的**真实探测未在无硬件环境验证**（只有模拟路径跑通）
2. `SPEC_TABLE` 规格值为**参考值**，须按官方 datasheet 复核
3. AMD Infinity Fabric 当前**未建模**（保守记为 pcie）
4. F5 为**模拟实现**，未接真实 K8s API
5. 成本系数仍为**占位值**，须真实数据替换
6. **未上传 GitHub**——待确认后推送
