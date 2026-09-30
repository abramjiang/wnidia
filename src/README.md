# WNIDIA · 异构算力调度基座

> 一套把「多档位异构算力 + 多推理引擎」统一成**一个可鉴权的 OpenAI 端点**的调度与治理基座。
> 面向 DGX Spark / GX10 单机与多机场景，按《Spark 云节点访问与使用手册》第八章红线做合规约束。

## 这个版本（v5）改了什么

**只有一件事：把 BP 里"推理数据留本地"从承诺变成代码。**
完整设计与两轮自查见 **`docs/PROMPT-PRIVACY.md`**。

| # | 改了什么 | 落在哪 |
|---|---|---|
| 1 | **prompt 默认不落库明文**：`tasks.prompt` 只写占位标记 + `prompt_digest` + `prompt_chars`；调度所需副本只活在控制面进程内存，任务终态即释放 | `controller/prompt_guard.py`、`controller/db.py` |
| 2 | **落库闸门放在 `db` 写入路径内部**，任何调用方都绕不过（含演示注入 / 抢占注入等旁路） | `db.upsert_task` / `upsert_tasks` |
| 3 | **对外出口统一字段级脱敏**：`/admin/state`、看板 `/api/state`、`/v1/tenant/{t}/usage`、审计证据包；脱敏后仍给摘要与长度（**可核验、不泄露**） | `main.py`、`dash.py`、`trust.py` |
| 4 | **两个开关是「与」关系**：`WNIDIA_STORE_PROMPT=1` 且 `WNIDIA_REVEAL_PROMPT=1` 才可能看到全文；短文本预览**永远留一截**不给 | `controller/config.py` |
| 5 | **派发前有闸**：拿到占位标记就显式判失败（`error=prompt_unavailable`），绝不拿占位符跑出"看着正常其实是假"的结果 | `harness._dispatchable`、`executor.execute` |
| 6 | 老库升级时**一次性抹掉历史明文**；新增列走既有迁移，不删库、不丢审计链 | `db.scrub_plaintext_prompts` + 启动钩子 |
| 7 | 顺带修掉三个真问题：Agent 把 prompt 放进**命令行参数**（`ps` 可见）、`smart-dispatcher` 的 Token 默认 `changeme` 导致经 Agent 调用必 401、看板 `/api/state` **绕过脱敏**（且 :8888 是公网映射端口） | `agent/app.py`、`skills/smart-dispatcher/`、`dash.py` |

> 要同时核实两件事：**库里确实没有明文**（直查 SQLite）、**功能没坏**（任务照常跑完）。
> 沙盒新增场景 N 用 11 条断言专门守这两点。

## v4 开发了什么

按 BP 逐页对齐后的开发计划执行，**高风险/高冲突项先不动**。
完整能力账本见 **`docs/CAPABILITIES.md`**，借鉴来源见 **`docs/GITHUB-REFERENCES.md`**，
三轮自查见 **`docs/BUG-HUNT-3ROUNDS.md`**。

| # | 对应 | 开发内容 | 落在哪 |
|---|---|---|---|
| 1 | BP §8 | **口径统一注册表**：对外只讲「云—边—端」三层；`secret=L1–L4`（数据密级）与 `cc=CC-L0–CC-L4`（机密计算层级）**彻底分离**；能力成熟度与对外措辞集中一处 | `controller/capabilities.py`、`/admin/capabilities` |
| 2 | **P5** | **三维路由补第三维「时延」**：节点上报 `latency_ms`，`latency_budget_ms` 为硬约束；不可满足时**独立拒绝原因** `latency_budget_unmet`（不混进"没算力"） | `controller/scheduler.py`、`worker/agent.py` |
| 3 | **P6** | **边缘算力箱**：预装清单 + 纳管命令 + 断网验收项 + 订阅档位建议 | `skills/edge-box-provisioner/` |
| 4 | **P6/P13/P14** | **断网自治**：worker 本地队列 + 指数退避 + 恢复自动回放；控制面**幂等**批次回放与**计量补偿** | `worker/agent.py`、`controller/offline.py` |
| 5 | **P7** | **家庭网格结算**：按有效算力 × 在线时长算机主分成与平台抽佣（幂等、显式标注为规划假设）；画像补在线率/稳定性/带宽 | `controller/settlement.py`、`/admin/settlement` |
| 6 | **P8** | **私有云规划**：按并发与合规等级出拓扑与三年 TCO，**vGPU 授权隐性成本置顶提示**；三种交付形态 | `skills/private-cloud-planner/` |
| 7 | **P9** | **机密计算层级与审计**：`CC-L0..CC-L4` 抽象 + 准入闸门（不达标不入候选）；审计**哈希链**与**可出示证据包**（默认不含 prompt 明文、篡改可检出）；本地 KMS 占位 | `controller/trust.py`、`/admin/trust/*`、`/admin/audit/*`、`skills/cc-auditor/` |
| 8 | **P10** | **真实 FinOps**：优先采信引擎 `usage`，缺失才估算并标注；三口径计费（token/node/seat）；租户/项目/部门分账；计量质量 `real_ratio`；订阅分档 P0/P1/P2 | `controller/metering.py`、`/admin/metering`、`/admin/subscriptions` |
| 9 | **P13** | **接入层**：租户自助用量与账单、只读运维门户、**零依赖 SDK**；L2 补 **SLA 优先级排队**与**分时复用策略** | `controller/db.py:queued_tasks_ordered`、`/admin/time-slice`、`/v1/tenant/{t}/usage`、`/portal`、`sdk/` |
| 10 | **P18** | **远期项目轻量版（仅供沙盒验证）**：QPU 抽象 + statevector 模拟、PQC 接口占位、具身机队（灰度分配 + 回传脱敏） | `controller/qpu.py`、`controller/fleet.py`、`/admin/qpu/*`、`/admin/fleet/*` |
| 11 | **P21** | **GO/NO-GO 门槛度量**：五条门槛同时报出目标值/窗口/样本量，样本不足**不得判 GO** | `controller/gates.py`、`/admin/gates`、`skills/gate-inspector/` |
| 12 | — | **沙盒验证器**：一键起栈、A–M 全能力逐项断言、结果落盘 JSON | `scripts/sandbox_v4.py` |
| 13 | — | **测试地基**：端口清场（只清本项目、绝不误杀）与进程组回收 | `tests/_clean.py` |

**本轮按指令完全没碰**：`prompt` 明文落库（BP 冲突点）、一云多芯/国产卡通用调度、
硬件 TEE 实际接入、系统级依赖安装。

### v3 已完成的升级（保留）

| # | 升级项 | 落在哪 |
|---|---|---|
| 1 | 引擎可插拔层（mock / ollama / vllm / tensorfold + 降级链） | `controller/engines.py` |
| 2 | Skill：engine-selector（确定性选路 + 一致性保证） | `skills/engine-selector/` |
| 3 | 合规守卫（默认拒绝：只有 8888/9000 可绑 `0.0.0.0`；弱口令拒启；禁内网探测；凭据脱敏） | `controller/compliance.py` |
| 4 | 可复现推理叙事（exact 引擎约束 + `consistency_satisfied` 标注） | `docs/PITCH.md` |
| 5 | 韧性补课（绑定超时回收、启动重试上限、抢占失败回滚、隔离期） | `controller/harness.py` |
| 6 | 评测真实性修复（补齐 4 个被静默忽略的断言键） | `skills/run_evals.py` |
| 7 | 测试与实现对齐（网关调用补 Token，消除假通过） | `tests/` |

## 快速开始（本机，无需 GPU）

```bash
# 1) 装依赖（建议 venv）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) 一键启动（默认只绑 127.0.0.1，安全）
WNIDIA_PY="$PWD/.venv/bin/python" bash scripts/run_local.sh
#   看板 :8888（Basic 鉴权） / API :9000（Bearer Token） / Agent :7000（仅回环）

# 3) 自检
WNIDIA_PY="$PWD/.venv/bin/python" python tests/e2e_test.py
WNIDIA_PY="$PWD/.venv/bin/python" python tests/adversarial_test.py
EVAL_PYTHON="$PWD/.venv/bin/python" python skills/run_evals.py

# 4) 全能力沙盒验证（自带栈，跑完自动关栈）
WNIDIA_PY="$PWD/.venv/bin/python" python scripts/sandbox_v4.py
```

## 部署到 Spark 云节点

```bash
NODE_NUM=51 WNIDIA_TOKEN=<强随机> WNIDIA_DASH_PASS=<强随机> \
  bash scripts/deploy_gx10.sh            # 或 MODE=gpu ENGINE=ollama / tensorfold
```

端口规律（手册 1.2）：`NN` 为节点编号（51–100），SSH = `6NN`、看板 = `8NN`（内网 8888）、
API = `9NN`（内网 9000）。**其它端口没有公网映射**，一律只绑 `127.0.0.1` 并用 SSH 隧道访问。
详见 `DEPLOY_SPARK.md` 与 `docs/COMPLIANCE.md`。

## 文档地图

| 文档 | 内容 |
|---|---|
| `docs/CAPABILITIES.md` | **v4 能力账本**：BP 对应关系、成熟度口径、新增接口一览 |
| `docs/GITHUB-REFERENCES.md` | **借鉴来源清单**：每个设计学自哪个项目、借鉴了什么、差在哪 |
| `docs/BUG-HUNT-3ROUNDS.md` | **v4 三轮自查**：19 个问题（7 高危）的现象 / 根因 / 修复 |
| `docs/ARCHITECTURE.md` | 分层架构、调度闭环、引擎可插拔契约、数据模型 |
| `docs/ENGINE-ADAPTER.md` | 引擎接入指南：新增一个引擎要做什么；TensorFold 接入的完整步骤与前置条件 |
| `docs/COMPLIANCE.md` | 手册第八章红线 → 代码/部署动作**逐条映射表** |
| `docs/FINOPS.md` | 计量与计费的记账口径、估算标注与出账纪律 |
| `docs/SCENARIOS-VALUE.md` | 可开发场景、应用价值、8 条变现路径、单位经济性 |
| `docs/PITCH.md` | 答辩口径：一句话定位、三位一体、常见质询与回答 |
| `docs/BUG-HUNT-3ROUNDS-v3.md` | v3 的三轮排查记录（历史） |
| `BENCHMARK.md` | 评测方法与真机 before/after（含沙盒验证结果章节） |
| `DEPLOY_SPARK.md` | 节点访问、部署、验证、运维 |

## 既有能力（未改动，继续复用）

- **调度闭环**：准入（配额/密级/主权/容量/时延/机密层级）→ 多目标打分匹配 → 绑定 → 轮询 → 完成计量
- **韧性三路径**：高优抢占 spot + checkpoint 续算；节点掉线重路由；多副本多数决 + 信誉分
- **JEV 决策增强层**：入口护栏、可选选择性校验、语义判定（默认 mock 离线占位）
- **9 个 Skill**：smart-dispatcher / gpu-slicer / resource-doctor / idle-onboarding /
  engine-selector / edge-box-provisioner / private-cloud-planner / gate-inspector / cc-auditor
- **演示注入**：`/admin/inject/preempt`、`/admin/inject/queue`、`/admin/node/lost`、`/admin/node/cheat`

## 合规提醒

本项目按比赛手册做**默认拒绝**设计：不带强口令、不限制绑定地址的"先跑起来"路径已被关闭。
任何需要跨出回环的访问，请优先使用 SSH 隧道（手册第五章），而不是把服务挂到 `0.0.0.0`。
