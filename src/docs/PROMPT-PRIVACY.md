# 推理数据（prompt）不留中心明文 —— 设计与两轮自查

> 对应任务：BP × 代码差距标注文档 §一·①「prompt 明文落库」，
> 原文列为 **P0 · 高风险但高收益**：改动量小、收益最大，也是最容易被一枪打穿的点。
>
> 本文件回答三件事：**为什么做、怎么做的、改完怎么证明它是对的**。

---

## 一、问题是什么（改前的真实状态）

| 事实 | 位置 |
|---|---|
| `main._derive()` 把用户消息拼成 prompt，塞进 `TaskSpec` | `controller/main.py` |
| `db.upsert_task()` 把 prompt **整段写进中心 SQLite** 的 `tasks.prompt` | `controller/db.py` |
| `/admin/state` 把全部历史 prompt **原样返回** | `controller/main.py` |
| 看板 `/api/state` 直连 DB，同样原样返回 | `controller/dash.py` |
| 看板 :8888 是手册里**有公网映射**的端口 | 《Spark 使用手册》1.2 |

结论：这不是"运维承诺"能盖住的问题 —— **任何持 Token/密码的人都能读到全部历史 prompt**。
BP P13 决策③明写「控制面/数据面分离、推理数据留本地」，与实现直接冲突。

---

## 二、怎么改的（设计）

### 2.1 核心取舍

调度**必须**拿到 prompt（worker `/start`、多数决 `/execute`、JEV 预检都要用），
所以不能简单删字段。方案是把它从"落库"改成"只在进程内存里活到任务终态"：

```
用户请求 ──► main._derive()  ──► TaskSpec(prompt=明文, prompt_digest, prompt_chars)
                                      │
                                      ▼
                        db.upsert_task()  ← 唯一落库闸门
                                      │
                    ┌─────────────────┴──────────────────┐
                    ▼                                    ▼
      STORE_PROMPT=0（默认）                 STORE_PROMPT=1（本地复盘）
      内存 spool 记一份明文                  库里直接写明文
      库里写 [REDACTED:prompt-not-stored]
                    │
                    ▼
      db._row_task() 读库时自动从 spool 补回 → 调度/校验链路无感
                    │
                    ▼
      任务终态 ──► prompt_guard.forget() 释放内存副本
```

### 2.2 三个关键决定

**① 落库闸门放在 `db` 的写入路径上，而不是调用方。**
`upsert_task()` / `upsert_tasks()` 内部统一调 `prompt_guard.accept()`。
这样**任何**调用方都绕不过去（包括演示注入端点 `/admin/inject/queue`、
抢占注入 `/admin/inject/preempt`），不会因为新加一条写入路径就漏。

**② 两个开关是「与」关系，不是「或」。**
- `WNIDIA_STORE_PROMPT=1` → 允许落库明文（默认 0）；
- `WNIDIA_REVEAL_PROMPT=1` → 允许对外返回全文（默认 0）；
- 只有**同时**为 1 才可能拿到全文（`prompt_guard.reveal_enabled()`）。

理由：单开关设计下，任何一次误操作（比如为了排查临时开了 reveal）都会直接泄露；
「与」关系把误操作的影响面压到最小。

**③ 派发前有一道闸，绝不用占位符去跑。**
改造后最大的风险不是"泄露"，而是**静默出错**：控制面重启后，未终态任务的
prompt 已经释放，如果照常派发，worker 会把 `[REDACTED:prompt-not-stored]`
当成真实输入去推理，得到**看着正常、其实是假**的结果，并且一路走到「完成」。

因此 `harness._dispatchable()` 在派发前检查，命中占位标记就
**显式判失败**（`error=prompt_unavailable`）并写事件说明原因。
`executor.execute()` 与 `_verify_majority()` 走同一道闸 —— 后者尤其重要：
拿占位符发给三个对端节点，会把"三个节点都回答同一段占位文本"当成「一致」，
等于用假数据给自己背书。

### 2.3 对外出口统一脱敏

对外输出统一走 `prompt_guard.redact_task()`，返回三种状态（**不留模糊表述**）：

| `prompt_state` | 含义 |
|---|---|
| `available` | 开关允许且原文在手（仅复盘模式 + 非终态任务） |
| `redacted` | 手里有原文，但按策略只给预览 |
| `released` | 原文已随任务终态释放，只能给摘要 |

无论哪种状态都补齐 `prompt_digest`（sha256 前 16 位）与 `prompt_chars`：
**不泄露内容，却仍能核验"这单用的是哪段输入、有多长"** —— 这是"脱敏"与
"什么都查不到"之间的关键区别。

预览规则也刻意反直觉：**永远留一截不给**。简单的 `text[:N]` 在短 prompt 上
等于全文，而短 prompt 恰恰最敏感（客户名、合同编号、口令片段）。
现在短文本最多给 75%，并标注「已脱敏 x/y 字符」。

### 2.4 迁移与老库

- 新增列 `prompt_digest` / `prompt_chars`，走既有 `MIGRATIONS`（`ALTER TABLE`），
  老库直接可升，不删库、不丢审计链。
- 启动时若 `STORE_PROMPT=0`，调用 `db.scrub_plaintext_prompts()` 把
  **历史遗留的明文一次性抹掉**（可用 `WNIDIA_SCRUB_LEGACY=0` 关闭），
  并在事件流与合规视图里报出清理条数。

### 2.5 新增配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `WNIDIA_STORE_PROMPT` | `0` | 是否把 prompt 明文落库 |
| `WNIDIA_REVEAL_PROMPT` | `0` | 是否允许对外返回全文（需与上者同时为 1） |
| `WNIDIA_SCRUB_LEGACY` | `1` | 启动时抹掉历史明文 |
| `WNIDIA_PROMPT_PREVIEW` | `48` | 预览字符数 |
| `WNIDIA_PROMPT_SPOOL_MAX` | `4000` | 内存 spool 上限（超出按最早淘汰并计数） |

---

## 三、代价（如实登记，不藏在注释里）

1. **控制面进程重启后，未终态任务的 prompt 不可恢复** → 这类任务会在派发前
   显式判失败，而不是"拿占位符跑出假结果"。这是「数据不留中心」的必然成本。
2. **`JEV_MODE=live` 时 prompt 会发往 JEV 服务**（默认 `127.0.0.1:8201`）。
   默认是 `mock`（纯本地、不出网）。用 live 就必须承认这一条，已写入
   `docs/COMPLIANCE.md` 的出站清单。
3. **prompt 会经网络发往承接任务的 worker 节点** —— 这是必需的，不是缺陷；
   但它意味着"数据不出域"只在"节点都在自己边界内"时才成立，答辩时必须讲清。
4. `prompt_digest` 用于比对同一输入，**不承诺抗枚举**（极短输入理论上可被穷举）。

---

## 四、两轮自查（共 10 个问题，4 高危）

### 第 1 轮 · 静态与一致性（编译 / pyflakes / 逐条查旁路）

| 编号 | 严重度 | 问题 | 修复 |
|---|---|---|---|
| **B5-01** | **高** | **Agent 把 prompt 明文作为命令行参数传给子进程**：`run_skill()` 用 `--task '<json含prompt>'`，`ps`、`/proc/<pid>/cmdline`、shell history 全都可见。v3 当初正是为了这个原因把 **Token** 移出 argv，却把 prompt 漏了 | Agent 改为写 **0600 临时文件**（`data/tmp`，`mkstemp`+`fchmod`），走 `--task-file`，`finally` 里 `unlink`；`--task` 仅留作人工调试并在帮助里注明 |
| **B5-02** | **高** | **`smart-dispatcher` 的 `--token` 默认值仍写死 `changeme`**，而其他 8 个 Skill 早已改成读 `WNIDIA_TOKEN` 环境变量；Agent 在 v3 就不再传 `--token` → **通过 Agent 调用该 Skill 必然 401**，再拿错误响应当集群状态去决策 | 统一改为 `default=os.getenv('WNIDIA_TOKEN','')` |
| **B5-03** | 中 | 同文件：控制面不可达/鉴权失败时直接 `r.json()` 当状态用 → 下游抛与真因无关的异常并打 traceback | 新增 `_load_state()`：离线文件/连接失败/非 200/非 JSON 一律返回**结构化错误**并 `exit 1` |
| **B5-04** | **高** | **看板 `/api/state` 直连 DB 返回 `t.to_dict()`，绕过 `/admin/state` 的脱敏** —— 而看板 :8888 是手册里**有公网映射**的端口，等于把 prompt 明文摆到公网 | 与 `/admin/state` 走同一套 `redact_task()`，并附 `prompt_policy` |
| **B5-05** | 中 | 自检时发现：**短 prompt 的"预览"等于全文**（`text[:N]` 在 `len ≤ N` 时整段返回），而短 prompt 恰恰最敏感 | 改为**永远留一截**：短文本最多给 75% + 标注「已脱敏 x/y 字符」 |
| — | — | （自查的测试自身也有错，一并记录）`U12` 最初用 `db.get_task()` 读回判断 scrub 是否生效 —— 但读库会从内存 spool 补回原文，**那是活任务的正常路径**，断言口径错了 | 改为直查 SQLite 核对**存储侧**；并在注释里说明为什么不能用读库结果判断 |

> 静态检查同时确认：全仓**没有**第二处绕过闸门的原始 SQL 写 prompt
> （`grep "SET prompt"` 只命中 `prompt_guard` 自己的清理语句）。

### 第 2 轮 · 端到端与边界

| 编号 | 严重度 | 问题 | 修复 |
|---|---|---|---|
| **B5-06** | 中 | 新增指标 `rehydrate_miss` **把"终态任务已按设计释放"和"活任务补不回（真问题）"混在一个计数里**：正常跑完的任务会让这个数一直涨，运维看到会误判成"内存机制坏了" | 拆成 `miss_active`（未终态补不回 = 真问题）与 `miss_released`（终态已释放 = 设计如此）；沙盒断言改为只看 `miss_active == 0` |
| **B5-09** | 中 | **空输入被异步转成 409**：派发闸门把空 prompt 判成 `prompt_empty` 失败后，网关回 `409 Conflict` —— 而这是**输入错误**，语义上应是 422；而且**会在任务列表里留下一条 FAILED 垃圾记录**（改前因为空 prompt 也会被照常派发，所以这条路径不存在） | 在 `/v1/chat/completions` 入口先校验：`messages` 为空或全空白 → **422** 直接拒绝，任务不入库；`_dispatchable` 的 `prompt_empty` 保留作纵深防御。对抗测试新增 C0 断言"不留 `prompt_empty` 任务记录" |
| **B5-10** | 中 | **沙盒对"节点已上报时延"只靠固定 `sleep(9)` 等待** —— 启动稍慢时会出现"只有 1 个节点上线、时延还是 0.0"，于是场景 B 的首条断言 B0 假失败、整个 B 场景提前 return（断言数从 75 掉到 71），看起来像被测系统坏了 | 新增 `wait_cluster_ready()`：**等条件**而非等秒数（≥3 节点在线且有节点已上报非零时延，上限 45s），并把就绪情况打印出来 |
| B5-07 | 低 | `data/tmp` 未加入 `.dockerignore` 与打包排除项 | 补 `.dockerignore`、打包排除、清理脚本 |
| B5-08 | 低 | 版本号与合规视图未同步（仍显示 4.0.0，且 `/admin/compliance` 没有 prompt 策略块） | 版本升 `5.0.0`；合规视图新增 `prompt_policy`（可投屏自证） |

### 第二轮验证结果

| 套件 | 结果 |
|---|---|
| `tests/prompt_privacy_test.py`（离线单测 12 条） | **12 / 12** |
| `scripts/sandbox_v4.py`（A–N，含新增隐私场景 N） | **75 / 75** |
| `tests/e2e_test.py` | **16 / 16** |
| `tests/adversarial_test.py`（A–S，含新增 C0 与 S1–S3） | 见包内同批次日志 |
| `skills/run_evals.py`（9 个 Skill） | **55 / 55** |
| `tests/jev_test.py` | **29 / 29** |

场景 N 的 11 条断言刻意"**既证数据没落库、又证功能没坏**"：

| 断言 | 证什么 |
|---|---|
| N1 任务仍正常完成 | 改造没有把功能搞坏（prompt 靠内存副本送达） |
| N2 直查 SQLite 无明文 | **数据确实没落库**（不是"接口层遮住了"） |
| N3 `/admin/state` 全字段扫描无敏感串 | 出口脱敏 |
| N4 看板 `/api/state` 同样脱敏 | B5-04 回归约束 |
| N5 租户用量同样脱敏 | 出口口径唯一 |
| N6 摘要 + 长度 + 状态齐全 | 脱敏不等于"什么都查不到" |
| N7/N8/N9 证据包：默认无明文、保留摘要、开关未开时如实拒绝 | 不给"看起来给了"的错觉 |
| N10 合规视图暴露策略 | 答辩可投屏自证 |
| N11 `miss_active == 0` | 内存副本机制真的有效（不是"跑通了但其实是假结果"） |

---

## 五、答辩口径（建议照这个说）

> "BP 写的是『推理数据留本地』。我们原来把 prompt 存在中心库、而且看板就能看到，
> 这是我们自己找出来的问题。现在的做法是：**prompt 默认根本不落库明文**，
> 库里只留一个占位标记加摘要和长度；调度要用的那份只活在控制面进程内存里，
> 任务一结束就释放。对外接口统一脱敏，摘要和长度照给 —— 你既能核验
> 『这单用的是哪段输入』，也拿不到内容。
> 代价我也说清楚：控制面重启后，没跑完的任务拿不回 prompt，
> 这类任务会被显式判失败，而不是拿占位符跑出个假答案。"

**能当场自证的三条命令**：

```bash
# 1) 策略视图（投屏）
curl -s -H "Authorization: Bearer $WNIDIA_TOKEN" \
  http://127.0.0.1:9000/admin/compliance | python3 -m json.tool | grep -A 12 prompt_policy

# 2) 直查库里没有明文
sqlite3 data/wnidia.db "SELECT task, substr(prompt,1,24) FROM tasks LIMIT 5;"

# 3) 脱敏输出仍可核验
curl -s -H "Authorization: Bearer $WNIDIA_TOKEN" http://127.0.0.1:9000/admin/state \
  | python3 -c "import json,sys; t=json.load(sys.stdin)['tasks'][-1]; \
print({k:t[k] for k in ('prompt','prompt_state','prompt_digest','prompt_chars')})"
```
