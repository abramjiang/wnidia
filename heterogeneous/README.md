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
| `profile_ab.py` | A/B 路由收益验证（M3）—— 决策质量 + 估算成本 | 8/8 |
| `phase_split.py` | Prefill / Decode 分离规划（M4）—— 含传输开销估算 | 9/9 |
| `pilot_5090_gb10.py` | **首个试点：5090 + GB10 完整可运行案例** | 9/9 |
| `device_discovery.py` | **F1** 设备自动发现与画像注册（含模拟） | 16/16 |
| `telemetry.py` | **F2** 统一遥测适配层（NVIDIA/AMD/Intel 归一化） | 8/8 |
| `compliance_policy.py` | **F3** 合规策略可配置化（替代硬编码） | 10/10 |
| `tenant_metering.py` | **F4** 租户 / 项目维度计量聚合 | 9/9 |
| `k8s_adapter.py` | **F5** K8s 接入（模拟）+ Prometheus 导出 | 8/8 |
| `proxy_gateway.py` | **P1** 透明代理网关 —— 调用方**零改动**接入 | 18/18 |
| `live_verify.py` | **P2** 真机验证 runner —— 产出可公开实测报告 | 18/18 |
| `routing_sdk.py` | **P3** 零依赖路由内核 —— 可被第三方直接采用 | 17/17 |
| `metering_report.py` | **P4** 计量账单 + 哈希链 + 反事实对比 | 18/18 |
| `README.md` | 本文件 | — |

**共 15 个模块。** 运行自检：`python3 controller/<模块名>.py`
运行试点：`python3 controller/pilot_5090_gb10.py`

> F1/F2/F5 的**硬件相关部分为模拟实现**（无硬件环境下可跑通），
> 真实接入时替换对应 provider 即可，上层业务逻辑无需改动。
>
> P1/P2 支持注入 `transport`；注入假传输层跑通的是**逻辑**，
> 且结果会被标记 `simulated=True`——**真机验证必须使用默认真实 HTTP 传输层**。
>
> P3 `routing_sdk.py` **只依赖标准库**，可单独复制到任何项目使用（已实测验证）。

配套文档：
- `docs/HETEROGENEOUS_ARCHITECTURE.md` —— 架构总纲
- `docs/HETEROGENEOUS_UPGRADES.md` —— 开发与功能文档（含可植入需求与应用场景）
- `docs/HETEROGENEOUS_DEVPLAN_M2M3M4.md` —— 完整开发规格（含硬件画像参考）
- `docs/HETEROGENEOUS_USAGE.md` —— 应用说明
- `docs/HETEROGENEOUS_CHECKLIST.md` —— 接入与测试清单
- `docs/HETEROGENEOUS_PILOT_5090_GB10.md` —— **首个试点案例说明**
- `docs/HETEROGENEOUS_P0_IMPLEMENTATION.md` —— **P0（F1–F5）实现开发文档**
- `docs/HETEROGENEOUS_SCORING_PATH.md` —— **提分路径（P1–P4）实现开发文档**

---

## 提分路径四模块（P1–P4）

针对"功能不少但缺少**能跑的证据**与**低门槛接入**"这一短板，补齐四项：

| 模块 | 解决什么 | 关键设计 |
|---|---|---|
| **P1 `proxy_gateway.py`** | 调用方要改造才能用 → 门槛高 | **伪装成调用方原本的服务**（OpenAI `/v1/chat/completions` 与 Ollama `/api/chat` 双线格式），改 base_url 即可，agent 零改动 |
| **P2 `live_verify.py`** | 没有真机实测数据 → 不可信 | 一条命令跑场景矩阵 + A/B，**产出可公开报告**；模拟数据强制标记 `simulated` |
| **P3 `routing_sdk.py`** | 能力被锁在自己架构里 | 抽成**零依赖单文件**，第三方可直接复制或接受贡献 |
| **P4 `metering_report.py`** | 有计量代码 ≠ 有计量产品 | 生成**账单**（按租户/项目/节点）+ 三种导出 + 哈希链 + 反事实对比 |

### P1 的"透明"具体指什么

```python
# 调用方原本这样写：
client = OpenAI(base_url='http://localhost:11434/v1')

# 接入 WNIDIA 后：把 11434 交给 WNIDIA 监听，
# 上面这行代码**一个字都不用改**，但请求已被画像路由 + 计量。
```

支持的透明端口：`ollama=11434`、`openai=8000`。

### P2 的诚实设计（最重要）

注入假传输层时，报告顶部会写：

> ⚠️ **本报告为模拟数据（注入的假传输层），不可对外发布。**

这杜绝了把仿真结果当实测对外发布的可能。

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
