# 架构说明

## 1. 四层视图

```
┌─────────────────────────────────────────────────────────────────────┐
│ L4 交互层                                                            │
│   Agent 应用层  agent/app.py        :7000（仅回环，SSH 隧道访问）      │
│   看板          controller/dash.py  :8888（公网映射 8NN，Basic 鉴权）  │
├─────────────────────────────────────────────────────────────────────┤
│ L3 控制面                                                            │
│   OpenAI 兼容网关  /v1/chat/completions   controller/main.py         │
│   Harness 闭环     harness.py（detect_lost→reclaim→launch→poll）      │
│   准入 / 打分 / 绑定  scheduler.py（★三维：密级 / 时延 / 成本）        │
│   ★ 引擎可插拔层   controller/engines.py                              │
│   ★ 合规守卫       controller/compliance.py                            │
│   ★ 口径注册表     controller/capabilities.py（三层 / secret·cc 分离） │
│   ★ 计量与分账     controller/metering.py                             │
│   ★ 结算           controller/settlement.py（机主分成 / 平台抽佣）     │
│   ★ 可信与审计     controller/trust.py（CC 层级 / 哈希链 / 证据包）    │
│   ★ 门槛度量       controller/gates.py（GO / NO-GO，含样本量检查）     │
│   ★ 断网自治批次   controller/offline.py（幂等回放 + 计量补偿）        │
│   ★ QPU 抽象       controller/qpu.py（statevector 模拟，simulated）    │
│   ★ 机队           controller/fleet.py（灰度分配 / 回传脱敏）          │
│   JEV 决策增强层   jevclient.py / jev_local.py                        │
│   存储             db.py（SQLite + WAL，带轻量迁移 + 审计哈希链）      │
├─────────────────────────────────────────────────────────────────────┤
│ L2 执行面（worker，节点内 8101+，一律只绑 127.0.0.1）                  │
│   注册 / 心跳 / 任务执行 / 抢占 / 续算 / 引擎调用 + 降级              │
│   ★ 断网自治：本地队列 + 指数退避 + 恢复自动回放                       │
│   ★ 画像上报：带宽 / 在线率 / 稳定性 / 时延 / 功耗 / 形态 / 代际 / CC  │
│   档位：cloud-0(prefill) / edge-1(decode) / cpu-1(cpu) / home-1       │
├─────────────────────────────────────────────────────────────────────┤
│ L1 推理引擎（OpenAI 兼容契约）                                        │
│   mock（进程内） │ ollama:11434 │ vllm:8101 │ tensorfold:8080         │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. 调度闭环（Harness）

`harness.run()` 每 `WNIDIA_LOOP_INTERVAL` 秒（默认 2s）执行一轮 `_tick()`：

1. **`detect_lost()`** — 心跳超时（默认 15s）判离线；在途任务重新排队，超过
   `MAX_ATTEMPTS`（默认 5）判失败并留事件。
2. **`reclaim_stuck_binding()`** ★新增 — 绑定后启动失败或 worker 无响应的任务，
   超过 `BINDING_TIMEOUT_S`（默认 60s）退化为重排队或失败。
   **原实现没有这一步，任务会永久停在 `binding`**。
3. **`launch_queued()`** — 取最多 6 个 `queued` 任务走
   `scheduler.schedule()` → `POST worker:/start`。worker 明确返回 `ok=false`
   也会被当作启动失败处理（原实现只看"没抛异常就算成功"）。
4. **`poll_running()`** — 拉 `worker:/status`，进度落库（否则看板恒为 0）；
   `done` → `_complete()`；`unknown` → 重新排队。

`_complete()` 之后：可选的多数决校验（`scheduler.majority` + JEV 语义判定）、
计量入账（`db.add_ledger` 同时累加配额 `used`）、以及恢复被本任务抢占的 spot 任务。

### 节点隔离期 ★新增

`/admin/node/lost` 或心跳超时会把节点置为 `lost` 并写入隔离期（默认 15s）。
隔离期内心跳**只更新指标、不改变在线状态**。原因是：worker 心跳间隔 5s，
若允许立即复活，"掉线→重调度"这条演示路径在 5 秒内就被抹平，评审根本看不清。
隔离期结束后由心跳自然恢复 `online`。

## 3. 引擎可插拔层

### 3.1 契约

任何引擎只要满足下面两点，就能被纳入统一调度：

```
必选：POST {base}/chat/completions   OpenAI 请求/响应体
可选：GET  {base}/models             探活 + 模型名发现
可选：GET  {health_url}              非标准健康端点（如 TensorFold /health）
```

### 3.2 目录（`ENGINE_CATALOG`）

每个引擎声明：`label / kind / default_base / default_model / backend /
exact（是否逐字节可复现）/ multi_node / needs_container / tier_affinity /
strengths / caveats / license`。

内置四个：`mock`（进程内）、`ollama`、`vllm`、`tensorfold`。

### 3.3 生效顺序

```python
engines.resolve()            # WNIDIA_ENGINE=auto|mock|ollama|vllm|tensorfold
# auto：MODE=gpu → WNIDIA_GPU_ENGINE（默认 ollama）；否则 mock
```

节点启动时解析一次，写入 `ENGINE_NAME`，并计算降级链
`FALLBACKS = bench_engine()` 中的前两个非自身项（默认 `tensorfold→vllm→ollama`,
真实引擎优先，`mock` 永远最后）。

### 3.4 运行时降级

`worker.engine_call()` 按「所选引擎 → 降级链 → mock」依次尝试：

- 成功且用的是首选引擎 → 原样返回；
- 成功但用了降级引擎 → 返回 `[degraded→<engine>] ...`，并置
  `ENGINE_STATE.degraded=True`；
- 全部失败 → 返回 `[degraded→mock] ...`。

**任何退化都会在应答文本和 `/healthz`、`/engine`、心跳里显式标注**，
绝不静默假装成功。心跳携带 `engine / engine_healthy / degraded` 三个字段，
落库到 `nodes` 表，`/admin/state` 与 `resource-doctor` 都能读到。

### 3.5 选路（`engines.select` / engine-selector Skill）

百分制确定性打分，同分按固定优先级 `tensorfold > vllm > ollama > mock` 打破平局，
**保证同一输入两次运行结果一致**。要点：

- 密级 L3/L4 或显式 `prefer_exact` ⇒ 非 `exact` 引擎 −25 分，且
  `consistency_satisfied=false`（不假装满足）；
- 探活失败 ⇒ −60 分，排到降级链末尾（仍列出，便于解释"为什么不用它"）；
- **只要有一台真实引擎探活通过，`mock` 额外 −45 分**。
  （★修复：否则 mock 因 `exact=True` 会在"要求可复现"时压过真实引擎，
  得出"选了一个不产出真实 token 的引擎"这种荒谬结论。）

## 4. 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `nodes` | `tier/role/trusted/sovereign_region/util/free_mem_gb/reputation/cheat` + ★`engine/engine_healthy/degraded` | 节点画像 |
| `tasks` | `sla/secret/cost/state/node/progress/attempts/answer` | 任务规约与运行期状态 |
| `events` | `ts/kind/message/node/task` | 调度事件流（看板与答辩的证据来源） |
| `ledger` | `tenant/task/node/cost/note` | 计量账本（写入时同步累加配额 `used`） |
| `quotas` | `tenant/quota/used` | 租户配额 |

**迁移**：`db.MIGRATIONS` 用 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`
做增量升级，老库可直接升到 v3，不需要 `rm data/wnidia.db` 丢证据。
`upsert_node` 已从"位置绑定 INSERT INTO nodes VALUES(...)"改为**显式列名**，
避免以后加字段导致错位。

## 5. 端口与绑定矩阵（★按手册修正）

| 服务 | 端口 | 绑定 | 公网 | 原因 |
|---|---|---|---|---|
| 控制面 API | 9000 | `0.0.0.0` | 9NN | 手册 4.1 唯一两个映射端口之一；必须带 Token |
| 看板 | 8888 | `0.0.0.0` | 8NN | 同上；必须带 Basic 鉴权 |
| Agent UI | 7000 | `127.0.0.1` | ✗ | 无映射；绑 `0.0.0.0` 只会暴露给同网段其他队伍 |
| worker ×3 | 8101-8103 | `127.0.0.1` | ✗ | 控制接口无鉴权，绝不外露 |
| 推理引擎 | 11434 / 8101 / 8080 | `127.0.0.1` | ✗ | 由控制面统一鉴权代理 |
| JEV 服务 | 8201 | `127.0.0.1` | ✗ | 同上 |

`compliance.assert_bind_allowed()` 会对"非 8888/9000 却要绑 `0.0.0.0`"直接抛错；
`worker._bind_check()` 在启动时做同样检查并 `SystemExit`。

## 6. 扩展点

| 想做什么 | 改哪里 |
|---|---|
| 接入新推理引擎 | `controller/engines.py` 的 `ENGINE_CATALOG` + `decide/degrade` 策略 |
| 新增一个 Agent Skill | `skills/<name>/{SKILL.md,tool.py,evals/evals.json}` + `agent/app.py` 的 `SKILLS` 与 `run_skill` |
| 换调度策略 | `controller/scheduler.py` 的 `admit/score/match` |
| 加一个节点档位 | `models.NodeTier` + `scheduler.score` 的档位偏好表 |
| 接国产卡/新后端 | 新增一个 engine 条目 + 一个 `backend` 标签；调度层无需改动 |
