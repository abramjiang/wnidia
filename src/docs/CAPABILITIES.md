# 能力清单：BP 对应关系、实现位置与口径

> 当前版本 **v5**（v4 的全部能力 + prompt 隐私改造）。

> 本文件是 v4 相对 v3 的**能力账本**。每一项都能在沙盒里跑出证据
> （`python scripts/sandbox_v4.py`），也都能在 `controller/capabilities.py`
> 的注册表里查到成熟度标注。
>
> 口径规则（**答辩必须遵守**）：
> - `已实现` = 代码在跑，且沙盒里有对应用例通过；
> - `部分实现` = 主干在跑，真机细节待验证；
> - `有决定无执行` = 策略可计算、可解释，**执行依赖硬件与真机验证**；
> - `沙盒模拟` = 接口与流程已在沙盒中验证，真机能力待接入；
> - `仅规划` = 已列规划，**尚未实现**（不得说成"已支持"）；
> - `明确不做` = 按战略边界不做。

---

## 1. 本轮开发范围与落地位置

| 任务（用户指令） | 对应 BP | 落地位置 | 成熟度 |
|---|---|---|---|
| P6 边缘算力箱 | P6 | `skills/edge-box-provisioner/`、`worker/agent.py`（画像补功耗/形态/带宽）、`controller/offline.py` | 已实现 / 沙盒模拟 |
| P7 家庭异构网格 | P7 | `controller/settlement.py`、`/admin/settlement`、`skills/edge-box-provisioner`（订阅档位建议） | 已实现 |
| P8 私有云交付 | P8 | `skills/private-cloud-planner/`（含 vGPU 授权隐性成本、三种交付形态） | 已实现 |
| P18 远期项目（部分） | P18 | `controller/qpu.py`（QPU 抽象 + statevector 模拟 + PQC 占位）、`controller/fleet.py`（机队/灰度/回传脱敏） | 沙盒模拟 |
| P21 门槛度量（轻量） | P21 | `controller/gates.py`、`/admin/gates`、`skills/gate-inspector/` | 已实现 |
| P13 接入层与治理层 | P13 | `controller/metering.py`、`/v1/tenant/{t}/usage`、`/portal`、`sdk/`、`/admin/time-slice` | 已实现 / 部分模拟 |
| P5 三维路由（补时延） | P5 | `controller/scheduler.py`（`latency_ms` 硬约束 + 打分项 + `REJECT_LATENCY`） | 已实现 |
| P9 机密计算（补层级与审计） | P9 | `controller/trust.py`、`/admin/trust/*`、`/admin/audit/*`、`skills/cc-auditor/` | 沙盒模拟 / 已实现（审计） |
| P10 FinOps（补真实计量） | P10 | `controller/metering.py`、三口径计费、分账维度、订阅分档 | 已实现 |
| BP §8 口径统一建议 | BP §8 | `controller/capabilities.py`（三层对外、secret/cc 分离、成熟度措辞） | 已实现 |

**本轮按指令完全没碰的两处高风险冲突点**：
1. `prompt` 明文落库（`tasks.prompt`）—— 仍保持 v3 现状，未改代码。仅在
   证据包导出侧默认排除 prompt 明文（`include_prompts=False`）。
2. 一云多芯 / 国产卡通用调度 —— 边界内不做，只保留"同一模型跨 CUDA ↔ Apple MLX
   的一致性适配"叙事。

---

## 2. 口径统一（BP §8 的三条建议）

### 2.1 分层：对外只讲三层

| 对外层 | 内部档位 | 说明 |
|---|---|---|
| `center` 中心层 | `cloud` | 私有云 / 区域池 |
| `edge` 边缘层 | `edge` | 算力箱 |
| `device` 端侧层 | `home` | 家庭异构网格 |
| —（不对外） | `cpu` | 内部兜底占位 |

代码：`capabilities.TIER_TO_LAYER` / `layer_summary()`；接口：`/admin/state.layers`。

### 2.2 密级：两套 L1–L4 彻底分离

| 符号 | 含义 | 取值 |
|---|---|---|
| `secret=L1–L4` | **数据密级**（任务侧） | 公开 / 内部 / 机密 / 绝密 |
| `cc=CC-L0–CC-L4` | **机密计算层级**（节点侧） | 无机密 / 机密 GPU / 远程证明 / 密钥与审计 / 部署可选 |

准入映射（`SECRET_TO_MIN_CC`）：`L3 → CC-L2`，`L4 → CC-L3`，其余 → `CC-L0`。

> 答辩要点：**禁止裸写"L3"**。BP 里的 L1–L4 指机密计算能力层级（CC），
> 代码里的 L1–L4 指数据密级（secret），同一个符号两套语义，说错就会被问倒。

### 2.3 能力成熟度：有决定无执行 must say so

`capabilities.OUTWARD_PHRASING` 固定了三段对外措辞，Agent 与看板都从这里取词：

- `有决定无执行` → "策略可计算、可解释；执行依赖硬件与真机验证"
- `沙盒模拟` → "接口与流程已在沙盒中验证；真机能力待接入"
- `仅规划` → "已列入规划，尚未实现"

---

## 3. 逐项能力说明

### P5 三维路由（时延 / 成本 / 密级）

- 节点画像新增 `latency_ms`（worker 用心跳往返时延实测并上报）。
- `latency_budget_ms > 0` 时是**硬约束**：超标节点不入候选；
  未上报时延（`latency_ms<=0`）的节点在有时延约束的任务上**不可信，直接排除**。
- 未设预算时，时延进入打分项（20ms 内满分 12 分，200ms 以上 0 分）。
- **不可满足的预算会被明确拒绝**，理由与"没机器"分开：
  `rejected:latency_budget_unmet`，事件里还带 `时延超预算×N` 的计数。

> 为什么要把拒绝原因分开：混在一起会让看板与答辩把"时延约束筛掉了节点"
> 说成"算力不足"，排查方向完全错。这是沙盒 B3 用例专门守的一点。

### P6 边缘算力箱

- `skills/edge-box-provisioner/`：Jetson AGX Thor / Orin 的规格、功耗预算、
  分层预装清单（JetPack / TensorRT / DeepStream / Fleet Command / WNIDIA）、
  可直接执行的纳管命令、**断网验收项**、订阅档位建议。
- 节点画像补 `power_w` / `form_factor` / `bandwidth_mbps` / `generation`。
- 断网自治（P6+P13/P14 交叉）：worker 侧本地队列 + 指数退避 + 恢复后自动回放；
  控制面 `/internal/offline/batch` **幂等**（同 `batch_id` 只记一次），
  并做计量补偿（`tokens_compensated`）。

### P7 家庭异构网格

- 节点画像补 `uptime_ratio`（在线率）、`stability`（稳定性），
  端侧节点承接 `sla-1` 强实时任务会被**扣分**（BP P7：强实时留在中心）。
- `controller/settlement.py` + `/admin/settlement`：按有效算力 × 在线时长结算，
  输出机主分成、平台抽佣、明细与**规划假设标注**（`assumption=true`）。
- 结算写入 `settlements` 表，重复执行**幂等**（同周期不重复计账）。

### P8 私有云交付

- `skills/private-cloud-planner/`：按并发路数与合规等级输出拓扑
  （管理/CPU/GPU/存储/备份节点数、机柜、冗余、25G 组网）、CAPEX 逐项、
  OPEX、三年 TCO，并把 **vGPU 授权年费作为隐性成本置顶提示**
  （`hidden_cost_warning`）；`L3/L4` 强制独立备份节点，`L4` 标注专属池。
- 三种交付形态：`lease`（租赁）/ `hosted`（托管）/ `onprem`（本地部署）。

### P13 推理数据（prompt）不留中心明文（v5）

- 默认**不落库明文**：`tasks.prompt` 只写占位标记 + `prompt_digest` + `prompt_chars`；
  调度所需副本只在控制面进程内存，任务终态即释放。
- 落库闸门放在 `db.upsert_task()` / `upsert_tasks()` 内部（**任何调用方都绕不过**）。
- 对外出口统一脱敏：`/admin/state`、看板 `/api/state`、`/v1/tenant/{t}/usage`、审计证据包。
- 两个开关是**「与」关系**：`WNIDIA_STORE_PROMPT=1` 且 `WNIDIA_REVEAL_PROMPT=1`
  才可能拿到全文；默认双关。
- 派发前有闸：拿到占位标记时**显式判失败**（`error=prompt_unavailable`），
  绝不拿占位符去跑出"看着正常其实是假"的结果。
- 独立报告：`docs/PROMPT-PRIVACY.md`（含两轮自查 8 个问题）。

### P9 机密计算层级与审计

- `controller/trust.py`：
  - `CC-L0..CC-L4` 层级抽象；`secret → cc` 准入闸门（`check_node`）接入
    `scheduler._candidates()`，**不达标或证明无效的节点不入候选**；
  - 证明签发/校验（沙盒后端 `sim`，真机需 NVTrust / CC 机型，**留接口不碰驱动**）；
  - 审计**哈希链**（前序哈希 + 内容哈希，参考 RFC 6962 / in-toto 思想）：
    `append_chain()` 保证"读链头 → 算哈希 → 落库"原子；
  - 证据包导出 `/admin/audit/export`（默认**不含 prompt 明文**）+ 签名校验，
    篡改可检出；
  - 本地 KMS 占位（`kms_sign` / `kms_verify`）。
- `skills/cc-auditor/`：把 `secret` 翻译成所需 `cc`，逐节点给出
  `level_ok / attestation_present / attestation_valid / verdict`，
  并校验审计链完整性、报出断链位置。

> 边界：**不检查也不触碰硬件 TEE**；沙盒中的证明是软件签发，
> 对外只能说"层级与证明是否满足当前要求"，不能说"已具备机密计算能力"。

### P10 FinOps：真实计量与分账

- `controller/metering.py`：
  - **优先采信引擎返回的 usage**；缺失时按字符/经验公式估算并标
    `estimated=true`；`metering_quality()` 给出 `real_ratio`（真实占比），
    对客户出账前应先把估算条目清零；
  - 三种计费口径并存：`token` / `node`（节点秒） / `seat`（席位）；
  - 分账维度：租户 / 项目 / 部门（`by_tenant` / `by_project` / `by_department`）。
- 订阅分档 `P0 / P1 / P2`（`/admin/subscriptions`）：
  **仅声明与展示，不做计费强制**（沙盒展示用），价格与能力包见
  `capabilities.SUBSCRIPTION_TIERS`。

### P13 接入层与治理层

- 统一网关 `/v1/chat/completions`（OpenAI 兼容，全量鉴权）+ 管理 API。
- **Token 计量网关**：网关侧统一记账，任务完成时落账本（含维度字段）。
- 租户自助：`/v1/tenant/{tenant}/usage`（只读用量 + 账单 + 质量）、
  `/portal`（只读门户页）。
- **SDK**：`sdk/wnidia_client.py`——零第三方依赖轻客户端，覆盖
  health / capabilities / metering / gates / tenant_usage / settlement 等接口。
- 纳管画像（L1）补带宽、在线率、稳定性、时延、功耗、形态、代际。
- 调度内核（L2）补 **SLA 优先级排队**（`db.queued_tasks_ordered()`：
  `sla-1 → sla-2 → sla-3`，同级 FIFO）与 **分时复用策略**
  （`/admin/time-slice`，输出 `execution=policy_only`）。
- 审计证据包（L3/L4）见 P9。

> 分时复用只到**策略**：真实的 MPS / MIG 隔离执行依赖硬件，GB10 不支持 MIG，
> 因此输出里显式带 `execution='policy_only'`。

### P18 远期项目（仅做"接口 + 沙盒验证"的轻量版）

| 子项 | 做法 | 明确边界 |
|---|---|---|
| QPU 资源抽象 | 受限门集电路 + 极简 statevector 模拟器；`shots` 采样 | **不引入量子栈、不押技术路线**；输出 `simulated=true` |
| PQC 预留 | 签名/校验接口占位（`hmac-sha256-placeholder`）+ 哈希链演示 | 明示 `ready=false`，**不代表已具备 PQC 能力** |
| 具身机队 | 机队注册 / 确定性灰度分配（robot 哈希）/ 任务派发 / **回传脱敏** | 仿真承载；真机需 Isaac / ROS 车体 |

- 回传脱敏覆盖 IP、邮箱、手机号。
- `POST /admin/qpu/submit` 对 `backend=cudaq`（未安装）**拒绝而非静默回退**，
  超上限量子位、未知门同样被拒。

### P21 GO / NO-GO 门槛度量

- `controller/gates.py` + `/admin/gates` + `skills/gate-inspector/`。
- 五条门槛（目标值取自 BP P21，**BP 明确标注为非行业基准**）：
  可用率 ≥ 90%、抽检一致率 ≥ 99%、需求匹配率 ≥ 60%、单台毛利 > 0、
  综合毛利 ≥ 60%。
- 沿用 OpenSLO 的 `objective / target / window` 三段式：**每条门槛都必须
  同时报出目标值、窗口与样本量**；`sample < min_sample` 时判
  `insufficient`，**不得判 GO**；任一门槛 no-go 则整体 no-go 并给出收缩动作。
- 成本类指标在离线快照下只报样本量、**不编造数值**。

---

## 4. 明确不做（按 BP 边界，本轮完全不碰）

| 项 | 原因 |
|---|---|
| 一云多芯 / 跨厂商通用调度 | BP 边界；只保留同一模型 CUDA ↔ Apple MLX 的一致性适配 |
| CPO 光互联产品化 | 只做拓扑兼容性证据，不做产品 |
| 自营 Token 工厂 / GPU 云 | 重资产且与客户争利 |
| 硬件 TEE 实际接入 | 需 CC 机型与驱动级变更，属系统级操作，本轮只留接口 |
| `prompt` 明文落库的改造 | **高风险冲突点，按指令先不动** |
| DCGM 遥测看板 | 属真机项，沙盒用本地采样替代（`ops.dcgm_telemetry = planned`） |

---

## 5. 新增/变更的接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/admin/capabilities` | 能力总表 + 口径 + 成熟度措辞（答辩可投屏） |
| GET | `/admin/compliance` | 合规自检 + **prompt 留存策略**（v5） |
| GET | `/admin/state` | 任务总览（prompt 字段级脱敏；`?reveal_prompt=1` 需双开关才给全文） |
| GET | `/admin/subscriptions` | 订阅分档 P0/P1/P2 |
| GET | `/admin/time-slice` | 分时复用排期（策略） |
| GET | `/admin/metering` | 计量汇总与分账维度、计量质量 |
| POST/GET | `/admin/settlement` | 机主结算（执行 / 查询） |
| GET | `/admin/trust` | 机密计算层级与证明视图 |
| POST | `/admin/trust/prove` \| `/admin/trust/verify` | 证明签发 / 校验 |
| GET | `/admin/audit/verify` | 审计哈希链完整性 |
| POST | `/admin/audit/export` | 审计证据包导出（默认无 prompt 明文） |
| GET | `/admin/gates` | 五条 GO/NO-GO 门槛 |
| GET | `/admin/offline` | 断网回放批次与计量补偿 |
| POST | `/admin/offline/simulate` \| `/admin/offline/recover` | 断网/恢复注入 |
| GET/POST | `/admin/qpu` \| `/admin/qpu/submit` | QPU 抽象与受限电路提交 |
| GET/POST | `/admin/fleet` \| `/admin/fleet/register|rollout|dispatch|uplink` | 具身机队 |
| POST | `/admin/inject/queue` | **演示注入**：同批入队（验证 SLA 优先级排队） |
| GET | `/v1/tenant/{tenant}/usage` | 租户用量与账单（只读） |
| GET | `/portal` | 客户运维门户（只读自助页） |
| POST | `/internal/offline/batch` | 边缘批次回放（幂等） |

---

## 6. 验证方式

```bash
# 全能力沙盒验证（自带栈，跑完自动关栈）
WNIDIA_PY=<python> python scripts/sandbox_v4.py

# 只跑指定场景（A 口径 / B 时延 / C 排队 / ... / M SDK）
WNIDIA_PY=<python> python scripts/sandbox_v4.py --only BC

# 保留栈以便手工继续看
WNIDIA_PY=<python> python scripts/sandbox_v4.py --keep
```

沙盒结果落盘在 `data/sandbox_v4_result.json`，逐条记录用例名、通过与否与细节。
