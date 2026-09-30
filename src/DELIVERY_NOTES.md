# 交付说明（修正版 v5）

> 文件名用 ASCII 是为了兼容 `zip`/`unzip` 在部分环境下的非 ASCII 文件名处理；
> 内容即中文交付说明。

本包在 `wnidia_v4`（按 BP 对齐开发 + 四轮自查修正）之上，完成了一项
**P0 高风险高收益的改造**：把 BP「推理数据留本地」从承诺变成代码，
并对该改造做了**两轮 bug 自查**。基线：`wnidia_v4.zip`。修正版：`wnidia_v5.zip`。

## 〇、v5.1 增量（2026-09-28）：NVIDIA 生态实调 + Agent 决策者改造

在 v5 基础上叠加三项能力，全部通过本机回归（详见文末验证记录）：

### 1. 真实 GPU 画像走 NVIDIA 官方 NVML（`worker/agent.py`）
- 采集优先级：**pynvml（NVML ioctl）→ nvidia-smi → None**，来源随响应自证
  （`GET /metrics/gpu` 的 `source` 字段）；
- NVML 提供进程级显存、SM 利用率、温度；GB10 统一内存的 `[N/A]` 路径仍被
  逐层 except 兜住，不中断心跳；
- `nvidia-ml-py` 进 requirements（纯 Python 无驱动依赖，无 GPU 机器闲置无害）。

### 2. 引擎目录扩到六后端（`controller/engines.py`）
- 新增 **Triton Inference Server**（TensorRT-LLM 后端）与 **NVIDIA NIM** 两条
  完整 catalog 条目（探活/降级/许可/边界全带）；
- `ORDER = [tensorfold, vllm, triton, nim, ollama, mock]`：探活通过即插即用，
  不通过被降级链自然跳过——不装服务也能讲"六后端"。

### 3. Agent 从"事后解说员"到"调度决策者"（LLM 提议 → 内核裁决）
- `agent/app.py` 新增 `POST /v1/agent/propose`：只在**候选集内**选节点，
  返回结构化 `{target_node, reason, confidence}`；LLM 不可用回落确定性规则并
  如实标 `source=rule`；最近 veto 理由回流进提示词（Agent 自我修正）；
- `controller/agent_policy.py` 裁决器：**复用** scheduler 的候选集生成
  （准入/显存/主权/密级/时延/机密全部硬约束）做成员校验，通过才采纳；
- 三档开关 `WNIDIA_AGENT_POLICY`：`off`（默认，零行为变化）/ `shadow`
  （后台线程留痕，派发零停顿）/ `enforce`（裁决通过即采纳，异常一律回落）；
- 新表 `agent_decisions` + `GET /admin/agent-policy`：提议采纳率、一致率、
  veto 分布——答辩话术从"它解释了调度"变成"它提议了 N 次，采纳 M 次，
  每次 veto 都有可解释理由"。

### v5.1 验证记录（本机，2026-09-28）
| 套件 | 结果 |
|---|---|
| 全能力沙盒 A–N | 75/75 |
| 端到端 | 16/16 |
| 对抗/边界 A–R | 40/40 |
| prompt 隐私 | 12/12 |
| JEV 单元 | 29/29 |
| Skill 评测 | 55/55（第一轮发现 engine-selector 对新增引擎的 fixture 缺口，已修复并回归） |
| Agent 策略单测（新） | 10/10（`tests/agent_policy_test.py`） |
| HTTP 边界探针（新） | 9/9（鉴权/422/空候选/1500 候选/并发 20） |
| 静态 | compileall + pyflakes 全仓通过 |

## 一、验证结果（本机实测）

| 套件 | 命令 | 结果 |
|---|---|---|
| **全能力沙盒（A–N）** | `WNIDIA_PY=<py> python scripts/sandbox_v4.py` | **75 / 75**（新增场景 N 共 11 条） |
| **prompt 隐私单测** | `python tests/prompt_privacy_test.py` | **12 / 12** |
| Skill 评测（9 个 Skill） | `EVAL_PYTHON=<py> python skills/run_evals.py` | 见包内同批次日志 |
| 端到端 | `WNIDIA_PY=<py> python tests/e2e_test.py` | 见包内同批次日志 |
| 对抗/边界（A–R） | `WNIDIA_PY=<py> python tests/adversarial_test.py` | 见包内同批次日志 |
| JEV 单元 | `python tests/jev_test.py` | 见包内同批次日志 |
| 静态 | `pyflakes` + `compileall`（全仓） | 通过 |
| 端口清场 | 测试结束后 `lsof -iTCP:9000,8888,7000,8101-8104` | 无残留 |

> `<py>` 指你环境里的 Python 解释器（需已装 `requirements.txt`）。
> 沙盒明细落盘 `data/sandbox_v4_result.json`。

## 二、本次改造（v5）：prompt 不留中心明文

**问题**：BP P13 决策③写「控制面/数据面分离、推理数据留本地」，但实现把 prompt
整段存进中心 SQLite，`/admin/state` 与**看板 `/api/state`（公网映射端口）**
都能读到全部历史 prompt。这是答辩最容易被打穿的点。

**做法**（不改调度语义，只改"留不留、给不看"）：

| # | 改动 | 位置 |
|---|---|---|
| 1 | prompt **默认不落库明文**：库里只写 `[REDACTED:prompt-not-stored]` + `prompt_digest` + `prompt_chars` | `controller/prompt_guard.py`、`controller/db.py` |
| 2 | 调度所需副本只活在**控制面进程内存**（spool），任务终态即释放；读库时自动补回，调用方无感 | 同上 |
| 3 | **落库闸门放在 `db` 写入路径内部** —— 任何调用方都绕不过（含演示注入 / 抢占注入等旁路） | `db.upsert_task/upsert_tasks` |
| 4 | **对外出口统一脱敏**：`/admin/state`、看板 `/api/state`、`/v1/tenant/{t}/usage`、审计证据包；脱敏后仍给摘要 + 长度 + 状态 | `main.py`、`dash.py`、`trust.py` |
| 5 | 两个开关是**「与」关系**：`WNIDIA_STORE_PROMPT=1` **且** `WNIDIA_REVEAL_PROMPT=1` 才可能看到全文 | `controller/config.py` |
| 6 | **派发前有闸**：拿到占位标记就显式判失败（`error=prompt_unavailable`） | `harness._dispatchable`、`executor.execute` |
| 7 | 老库升级时**一次性抹掉历史明文**（`WNIDIA_SCRUB_LEGACY=1` 默认开） | `db.scrub_plaintext_prompts` + 启动钩子 |
| 8 | 短文本预览**永远留一截**不给（简单的 `text[:N]` 在短 prompt 上等于全文） | `prompt_guard.preview` |

**新增配置**：`WNIDIA_STORE_PROMPT`（默认 0）、`WNIDIA_REVEAL_PROMPT`（默认 0）、
`WNIDIA_SCRUB_LEGACY`（默认 1）、`WNIDIA_PROMPT_PREVIEW`（默认 48）、
`WNIDIA_PROMPT_SPOOL_MAX`（默认 4000）。

**可当场自证的三条命令**：

```bash
# 1) 策略视图（答辩投屏）
curl -s -H "Authorization: Bearer $WNIDIA_TOKEN" \
  http://127.0.0.1:9000/admin/compliance | python3 -m json.tool | grep -A 12 prompt_policy

# 2) 直查库里没有明文
sqlite3 data/wnidia.db "SELECT task, substr(prompt,1,24) FROM tasks LIMIT 5;"

# 3) 脱敏输出仍可核验（有摘要与长度，没有内容）
curl -s -H "Authorization: Bearer $WNIDIA_TOKEN" http://127.0.0.1:9000/admin/state \
  | python3 -c "import json,sys; t=json.load(sys.stdin)['tasks'][-1]; \
print({k:t[k] for k in ('prompt','prompt_state','prompt_digest','prompt_chars')})"
```

## 三、两轮 bug 自查（共 8 个问题，4 高危）

完整清单见 `docs/PROMPT-PRIVACY.md`。最值得看的五条：

| 编号 | 问题 | 为什么危险 |
|---|---|---|
| **B5-04** | **看板 `/api/state` 直连 DB 返回 prompt 明文，绕过了 `/admin/state` 的脱敏** | 看板 :8888 正是手册里**有公网映射**的端口 —— 等于把 prompt 明文摆到公网 |
| **B5-01** | Agent 把 prompt 作为**命令行参数**传给子进程（`--task '{"prompt":...}'`） | `ps`、`/proc/<pid>/cmdline`、shell history 全可见；v3 当初正是为这个原因把 Token 移出 argv，却把 prompt 漏了 |
| **B5-02** | `smart-dispatcher` 的 `--token` 默认仍是 `changeme`，其他 8 个 Skill 早已改成读环境变量 | **经 Agent 调用该 Skill 必然 401**，再拿错误响应当集群状态去决策（长期潜伏的功能缺陷） |
| **B5-06** | 新增指标 `rehydrate_miss` 把"终态已按设计释放"与"活任务补不回（真问题）"混在一个计数里 | 正常跑完的任务也让这个数一直涨，运维会误判成"内存机制坏了"；**指标不能只会报警、不会区分预期与异常** |
| **B5-05** | 自检发现：**短 prompt 的"预览"等于全文** | 短 prompt 恰恰最敏感（客户名、合同编号、口令片段），脱敏形同虚设 |

> 自查也发现**测试自身**的口径错误（U12 用读库结果判断 scrub 是否生效 ——
> 而读库会从内存补回原文，那是活任务的正常路径），已改为直查 SQLite 核对存储侧，
> 并把原因写进注释。

## 四、如何快速验证

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# ① 一键跑通全能力 + 隐私（自带栈，跑完自动关栈）
WNIDIA_PY="$PWD/.venv/bin/python" python scripts/sandbox_v4.py

# ② 隐私单测（不用起栈，覆盖"两个开关的与关系"与派发闸门）
python tests/prompt_privacy_test.py

# ③ 全量回归
export WNIDIA_PY="$PWD/.venv/bin/python" EVAL_PYTHON="$PWD/.venv/bin/python"
python tests/e2e_test.py && python tests/adversarial_test.py \
  && python skills/run_evals.py && python tests/jev_test.py
```

本地启动（默认只绑 127.0.0.1）：

```bash
WNIDIA_PY="$PWD/.venv/bin/python" bash scripts/run_local.sh
#   看板 :8888（Basic） / API :9000（Bearer） / Agent :7000（仅回环）
```

若确实需要在本地复盘时看到 prompt 原文（**仅限本机**）：

```bash
WNIDIA_STORE_PROMPT=1 WNIDIA_REVEAL_PROMPT=1 \
WNIDIA_PY="$PWD/.venv/bin/python" bash scripts/run_local.sh
```

## 五、部署到 Spark 云节点

```bash
unzip -o wnidia_v5.zip && cd wnidia_v5
NODE_NUM=<51-100> \
WNIDIA_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))") \
WNIDIA_DASH_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))") \
MODE=gpu ENGINE=ollama bash scripts/deploy_gx10.sh
```

- 公网端口按手册 1.2 换算：SSH `6NN`、看板 `8NN`、API `9NN`；**只有这两个服务端口有映射**。
- Agent :7000 与引擎 :8080 只绑回环，用 SSH 隧道访问。
- **不要在生产/公网场景打开 `WNIDIA_REVEAL_PROMPT`**。

## 六、仍存在的边界（如实说明，不要在答辩中含糊）

1. **prompt 会经网络发往承接任务的 worker 节点** —— 这是必需的（worker 要拿它推理）。
   因此"数据不出域"只在"计算节点都在自己边界内"时成立。
2. **`JEV_MODE=live` 时 prompt 会发往 JEV 服务**（默认 `127.0.0.1:8201`）。
   默认是 `mock`（纯本地、不出网）；切 live 就必须承认这条。
3. **控制面进程重启后，未终态任务的 prompt 不可恢复** —— 这类任务会在派发前
   **显式判失败**，而不是拿占位符跑出"看着正常其实是假"的结果。
   这是「数据不留中心」的必然成本。
4. `prompt_digest` 用于比对同一输入，**不承诺抗枚举**。
5. **未接入硬件 TEE**（需 CC 机型 + 驱动级变更）；沙盒内证明为软件签发。
6. **未采集真机性能数据**：`BENCHMARK.md` §3/§8 表格保留【待真机填写】。
7. **未做 DCGM 遥测看板**（`planned`）、**未验证真实跨机 `--tp 2`**、
   **生产级多租户仍为逻辑隔离**（非强隔离）。
8. **未做一云多芯 / 国产卡通用调度**（BP 边界，明确不做）。

## 七、目录速查

```
wnidia_v5/
├── README.md                    总览（v5 改造 + v4 开发）
├── DELIVERY_NOTES.md            本文件
├── DEPLOY_SPARK.md              部署与验证（已按手册修正端口/绑定/凭据）
├── BENCHMARK.md                 评测方法（含全能力沙盒验证章节）
├── controller/                  prompt_guard ★v5 / capabilities / metering / settlement /
│                                trust / gates / offline / qpu / fleet / db / scheduler / harness / dash
├── worker/agent.py              引擎调用 + 降级链 + 断网自治 + 画像上报
├── agent/app.py                 9 个 Skill 的注册与路由（v5：任务走 0600 临时文件，不进 argv）
├── skills/                      9 个 Skill；每个含 SKILL.md + tool.py + evals/ (+ fixtures/)
├── sdk/wnidia_client.py         零依赖轻客户端
├── scripts/                     sandbox_v4.py ★（A–N）/ run_local.sh / deploy_gx10.sh …
├── tests/                       prompt_privacy_test.py ★v5 / e2e / adversarial / jev / _clean.py
└── docs/                        PROMPT-PRIVACY ★v5 / CAPABILITIES / COMPLIANCE /
                                 BUG-HUNT-3ROUNDS（v4，含 -v3 历史）/ GITHUB-REFERENCES /
                                 FINOPS / ARCHITECTURE / ENGINE-ADAPTER / SCENARIOS-VALUE / PITCH
```
