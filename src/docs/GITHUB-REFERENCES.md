# 借鉴来源清单（v4）

> 本文件回答一个问题：**v4 新增的这些设计，是从哪里学来的？**
>
> 原则：本仓库**不拷贝第三方代码**。下面每一项都只借鉴"数据结构 / 协议格式 /
> 算法思路 / 工程约束"，落地方式全部按本项目场景重写，并且都写成纯 Python 标准库
> 实现（除 FastAPI/requests 外无新增三方依赖）。许可证与星级为查证时的公开信息，
> 仅供标注来源，**不代表本项目与之存在代码或授权关系**。

---

## 1. 边缘断网自治（P6 / P13 / P14）

| 来源 | 许可证 | 借鉴了什么 | 落在哪 |
|---|---|---|---|
| **KubeEdge** | Apache-2.0 | 边云协同的"云端不可达时边缘自治、恢复后补齐"模型：边缘侧持有本地状态与待同步队列，云端恢复后按批次同步而不是逐条重试 | `worker/agent.py` 的 `OFFLINE` 本地队列 + `_replay_offline()`；控制面 `controller/offline.py` 的批次回放入口 |
| **OpenYurt** | Apache-2.0 | "边缘自治单元"的概念：断网时以本地单元为准继续服务 | 同上；`/offline/status` 暴露 `active / pending / tokens / seconds` |
| **EdgeFlow / 通用边缘模式** | — | **指数退避**重连（避免断网时打爆控制面） | `worker/agent.py` `heartbeat_loop()`：`backoff = min(60, backoff*2)`，连续失败 3 次进入自治态 |

**关键差异（不是照抄）**：借鉴的都是"控制面观测与同步"的思路。本项目额外做了
**批次幂等**（同一 `batch_id` 只记一次）与**计量补偿**
（`tokens_compensated`）——因为断网期间的算力必须能进账单，否则机主白干。
幂等保证按 `batch_id` 去重落库，`controller/db.py:offline_batch_seen`。

---

## 2. 审计哈希链与证据包（P9 · CC-L3）

| 来源 | 许可证 | 借鉴了什么 | 落在哪 |
|---|---|---|---|
| **Certificate Transparency（RFC 6962）** | IETF RFC | "前序哈希 + 内容哈希"逐条链接、事后可重算校验的透明日志结构 | `controller/db.py:append_chain()`、`controller/trust.py:verify_chain()` |
| **in-toto** | Apache-2.0 | 证明（attestation）与"证据包"的格式思想：待证内容做摘要、摘要再签名、校验方重算摘要 | `controller/trust.py:export_evidence_pack()` / `verify_evidence_pack()`（含 `pack_digest` / `pack_signature` / `chain_verification` 三段） |
| **Sigstore 的"默认不包含敏感原文"实践** | Apache-2.0 | 对外出示的产物默认只含摘要与元数据，不含原始载荷 | 证据包默认 `include_prompts=False`，`prompts_included=false` |

**关键差异**：CT/in-toto 解决的是"跨组织公开可验证"。本项目只需要"同一控制面
内部可自证 + 可对监管出示"，因此实现上刻意做了简化（单文件 SQLite、前序哈希链），
但保留了**篡改必然破坏后继链**这一核心性质。
`tests/adversarial_test.py` 的 R21 与 `skills/cc-auditor` 的 `cc-chain-tamper-detected`
专门验证"改了内容能被检出、并能报出断链位置"。

---

## 3. 计量与计费维度（P10 · FinOps）

| 来源 | 许可证 | 借鉴了什么 | 落在哪 |
|---|---|---|---|
| **OpenMeter** | Apache-2.0 | **用量事件（usage）与账单维度（tenant / project / service）分开建模**：事件只记事实，账单维度是可配置的聚合视角 | `controller/db.py` ledger 增维度字段；`controller/metering.py:summarize()`（`by_tenant` / `by_project` / `by_department` / `by_billing_mode`） |
| **FinOps FOCUS 开放口径** | CC BY 4.0 | 用量与成本必须**分列**、并标明口径与来源（真实 vs 估算） | `metering_quality()` 给出 `real / estimated / real_ratio`；任务级 `metering_estimated` 标记 |
| **Lago** | AGPL-3.0（仅参考思路） | 多计费模式并行（按量 / 按时长 / 按席位）共用同一事件源 | 三口径 `token` / `node`（节点秒）/ `seat` |

**关键差异**：本项目不引入 OpenMeter/Lago 的服务端组件（它们是重的独立服务），
只把它们的数据划分方式落到一张 SQLite 的表设计上。同时加了一条本项目特有的
纪律：**估算条目必须显式标注**，并在对客户出账前清零（`metering_quality` 会报出来）。

---

## 4. 门槛度量（P21）

| 来源 | 许可证 | 借鉴了什么 | 落在哪 |
|---|---|---|---|
| **OpenSLO** | Apache-2.0 | SLI/SLO 的 `objective / target / window` 三段式，以及"没有足够样本就不能下结论"的工程纪律 | `controller/gates.py`：每条门槛必须同时给出 `value / target / window / sample`，`sample < min_sample` 判 `insufficient`，**不得判 GO** |

**关键差异**：OpenSLO 面向服务可用性。本项目的门槛是"经营 + 质量"混合
（可用率 / 抽检一致率 / 需求匹配率 / 单台毛利 / 综合毛利），
因此把 OpenSLO 的"目标 + 窗口"扩成"目标 + 窗口 + 样本量 + 收缩动作"。

---

## 5. 具身机队灰度（P18）

| 来源 | 许可证 | 借鉴了什么 | 落在哪 |
|---|---|---|---|
| **ROS 2 生态的机队管理实践（如 `ros2_control` / fleet 管理工具）** | Apache-2.0 | 机队按"稳定哈希分桶"做分阶段灰度，保证**同一台车在同一比例下归属稳定**（不来回跳版本） | `controller/fleet.py:_assign()`：`sha1(robot_id) % 100 < rollout_pct` |
| **SaaS 灰度发布通用模式（如 Flagger 类项目的阶段化思路）** | Apache-2.0 | 10% → 50% → 100% 的阶段化推进与"阶段化计划"登记 | `plan_stages=True` 输出三阶段分配预览 |

**关键差异**：真实车队的灰度必须考虑电池、位置、在线状态（本项目已把
`battery` / `region` 纳入注册信息），但**调度执行**依赖真机（Isaac / ROS），
本版只做"分配决策 + 回传脱敏"，输出里明确标 `simulated`。

---

## 6. 量子接口（P18）

| 来源 | 许可证 | 借鉴了什么 | 落在哪 |
|---|---|---|---|
| **通用 statevector 模拟器原理（量子计算教科书级算法：态向量 + 门矩阵作用 + 采样）** | — | 极简态向量模拟：`h` / `x` / `y` / `z` / `cx` / `measure` 受限门集 | `controller/qpu.py`，~2^n 态向量、`shots` 采样出计数 |
| **CUDA-Q（NVIDIA）** | Apache-2.0 | 后端的**枚举与拒绝语义**：未安装就不静默回退，明确报错 | `/admin/qpu/submit` 对 `backend=cudaq` 返回 `ok=false` + 明确 reason |

**关键差异**：本项目**不引入量子技术栈、不押技术路线**。这里的目标只有一个：
把"混合任务接口"抽象出来（QPU 与 GPU 任务共用同一套提交/准入/计量骨架），
并在沙盒里证明接口可用。因此在输出里固定带 `simulated=true`，
PQC 固定带 `ready=false`。

---

## 7. 其他工程约束（非项目来源）

| 实践 | 来源 | 落在哪 |
|---|---|---|
| 端口清场时"只杀自己人、绝不误杀" | 自建纪律（前一轮踩坑教训） | `tests/_clean.py`：lsof 取监听者 + **端口指纹**双重确认，任一不满足就不动 |
| 失败原因必须可区分（不能都叫"没算力"） | 自建纪律 | `Admission.REJECT_LATENCY` 与 `REJECT_CAP` 分离 |
| 测试必须先证明"测试本身可信" | 自建纪律（v3 的假绿教训） | `run_evals.py` 补齐全部断言键；`_clean.py` 可单独运行自证 |

---

## 8. 一句话总结

借鉴集中在四类**数据与协议形态**上：边云同步的队列模型（KubeEdge/OpenYurt）、
透明日志的哈希链（CT/in-toto）、用量与账单维度分离（OpenMeter/FOCUS）、
目标+窗口+样本量的度量纪律（OpenSLO）。**没有一处是拷贝代码**，
全部按本项目的约束（单机 SQLite、无新增重依赖、沙盒可验证）重写。
