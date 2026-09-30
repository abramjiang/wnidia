# 三轮 Bug 排查报告（v3 修正版）

> 排查对象：`wnidia_v2`（合并上游后的版本）
> 方法：**第一轮静态审查 → 第二轮本地全链路冒烟 → 第三轮边界/对抗/合规**
> 每轮都"先让断言真的跑起来，再修问题"，而不是先改代码再补测试。
> 最终状态：`e2e_test 16/16` · `adversarial_test 14/14` · `jev_test 29/29` · `skills/run_evals 32/32`

---

## 第 0 轮（前置）：先修评测本身，否则后面全是假绿

**这条最重要，单列。** 原 `skills/run_evals.py` 的 `check_expect()` 只实现了 6 个断言键，
但各 skill 的 `evals.json` 里实际写了 10 个：

| 断言键 | 原状态 | 后果 |
|---|---|---|
| `exit_code` / `exit_code_nonzero` | 已实现 | — |
| `json_assert` / `json_keys` / `json_nonempty` / `json_contains` / `json_candidate_nodes` | 已实现 | — |
| **`json_instance_count`** | ❌ 静默忽略 | gpu-slicer 的"实例数"从未被校验 |
| **`json_max`** | ❌ 静默忽略 | "利用率上限 ≤0.92"从未被校验 |
| **`json_contains_codes`** | ❌ 静默忽略 | resource-doctor 的诊断码从未被校验 |
| **`no_traceback_leak`** | ❌ 静默忽略 | 栈泄漏从未被发现 |

**结论：此前"20/20 全绿"含水分。** 补齐实现后立刻红了 2 条，就是下面 BUG-1、BUG-2。

其余同源问题：
- 缺 `evals/evals.json` 的目录会让整轮评测直接抛异常中断 → 改为 SKIP 并告警；
- 无法指定解释器，评测环境与部署环境不一致 → 新增 `EVAL_PYTHON` 覆盖；
- 计算 `python` 若没装 `requests`，`from controller import engines` 失败会导致
  engine-selector 整个跑不起来 → `engines.py` 改为**延迟导入 requests**（目录与选路是纯计算，
  不该被网络库绑架）。

---

## 第一轮：静态审查

### BUG-1（功能）`majority()` 平票时结果不可复现
- **位置**：`controller/scheduler.py:127`
- **原代码**：`best = max(set(answers), key=answers.count)`
- **问题**：`set` 迭代顺序由哈希决定，平票时"多数"是任意的 → 同一批答案两次运行
  可能得到不同结论，甚至因此惩罚不同的节点。这与本项目"确定性决策"的核心主张冲突。
- **修复**：改为 `sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]`，
  先按出现次数、再按字典序，**完全确定**。

### BUG-2（健壮性）诊断工具抛 traceback 而不是结构化失败
- **位置**：`skills/resource-doctor/tool.py:67`
- **问题**：状态文件不存在时 `open()` 直接抛 `FileNotFoundError`，栈打到 stderr；
  节点对象缺 `util`/`free_mem_gb`/`reputation` 等字段时 `KeyError`。这正是
  `no_traceback_leak` 断言要抓的东西。
- **修复**：`main()` 整体 try/except → 打印 `{"error": "..."}` 到 stdout 并 `exit 1`；
  `diagnose()` 用 `g()` 取值助手做字段容错。

### BUG-3（崩溃）worker 无法启动——`resume` 路径签名不符
- **位置**：`controller/scheduler.py:112` `resume(t, node)` 内部写 `node.node if isinstance(...)`
- **问题**：参数名与用法不一致（形参叫 `node` 却当 `NodeProfile` 用），调用方传字符串时
  其分支判断混淆，可读性差且极易在重构中崩溃。同一文件的 `_candidates()` 里
  `want_role` 还被赋值后从未使用（死变量），说明这里的功能曾被改动而未清理。
- **修复**：抽出 `want_role_for(t)` 供 `score()`/`_candidates()` 共用；
  `resume()` 显式规范化节点标识。

### BUG-4（可用性/正确性）密钥非法值打断调度闭环
- **位置**：`controller/scheduler.py:19,39,41`
- **问题**：`SECRET_RANK[t.secret]` 直接下标。一旦 `secret` 是 `L5` 或任意字符串
  （网关的 `_derive()` 允许 `secret` 由请求指定），`KeyError` 会从调度线程抛到
  harness 主循环——虽然有兜底，但该任务会一直无法被处理。
- **修复**：新增 `rank_of(secret)`，非法值按最低密级处理。

### BUG-5（数据层）`INSERT ... VALUES` 位置绑定，加字段必错位
- **位置**：`controller/db.py:66` `upsert_node()`
- **问题**：不带列名的 `INSERT INTO nodes VALUES(...)` 依赖 dataclass 字段顺序与表结构
  严格一致。本次要新增 `engine/engine_healthy/degraded` 三列，位置绑定必然错位。
- **修复**：改为**显式列名** + 命名参数；新增 `MIGRATIONS` 用
  `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 做增量迁移（老库不必删库重来）。

### BUG-6（合规，高危）`idle-onboarding` 可被诱导扫描内网其他节点
- **位置**：`skills/idle-onboarding/tool.py:21`（探活）+ `agent/app.py:115`（从用户话术抽 IP）
- **问题**：`--worker-host` 完全来自用户输入，Agent 层还会用正则从自然语言里抽 IP
  直接透传。也就是说，**对着 Agent 说一句"把 <LAN_IP> 纳管进来"，就会真的向
  另一支队伍的节点发请求**——手册 8.1-2 明令禁止，且所有网络行为有日志。
- **修复**：新增 `controller/compliance.py::assert_probe_allowed()`，显式封禁
  `<LAN_SUBNET>/24`，非回环目标必须进 `WNIDIA_ALLOW_HOSTS` 白名单；
  `idle-onboarding` 在**发起任何请求之前**校验，不通过就直接返回
  `blocked_by_compliance: true` 并说明依据。合规模块不可用时**保守拒绝**。

### BUG-7（安全）引擎启动命令硬编码 `--host 0.0.0.0`
- **位置**：`skills/gpu-slicer/tool.py:13`
- **问题**：生成的 `vllm serve` 命令绑全网卡。8101 不是公网映射端口（手册 4.1），
  绑 `0.0.0.0` 只会把无鉴权的推理服务暴露给同网段的其他 49 支队伍。
- **修复**：改为 `--host 127.0.0.1`，并在输出里附 `access_hint` 说明 SSH 隧道用法。

### BUG-8（安全）凭据出现在命令行参数里
- **位置**：`agent/app.py` 的 `run_skill()`（`--token`）、各 skill 的 `--token` 默认值
- **问题**：`subprocess.run([..., '--token', TOKEN])` 会让 Token 出现在 `ps`、
  `history`（以及某些日志）里；默认值还是字面量 `'changeme'`。
- **修复**：改为通过子进程**环境变量** `WNIDIA_TOKEN` 传递；各 skill 的 `--token`
  默认值改为 `os.getenv('WNIDIA_TOKEN', '')`。

### BUG-9（安全）Agent 页面反射 Token 未转义
- **位置**：`agent/app.py:356` `PAGE.replace('__TOKEN__', token)`
- **问题**：Token 被直接拼进 HTML 属性，含引号/尖括号的 Token 可破坏页面结构。
- **修复**：`html.escape(token, quote=True)`；同时补 `run_skill` 的
  路径穿越规范化校验（`realpath` + 前缀断言）。

### BUG-10（部署，高危）部署脚本明文打印凭据 + 硬编码旧环境 IP
- **位置**：`scripts/deploy_gx10.sh:136-138`、`deploy_spark.sh:56-57`
- **问题**：① 终端直接打印 Token 与看板密码——一旦截图/录屏（比赛演示太常见）
  就是手册 8.1-3 违规；② 公网 IP/端口写死 `<PUBLIC_IP> / 8026 / 9026 / 7026`，
  与手册的 `<PUBLIC_IP_2> / 8NN / 9NN` 完全不符，照着用必然连不上。
- **修复**：① 新增 `mask()` 脱敏，凭据只回显掩码；`env.sh` 权限收紧 `600`；
  ② 按手册 1.2 用 `NODE_NUM` 换算公网端口（`6NN / 8NN / 9NN`），去掉所有硬编码旧地址。

### BUG-11（合规）默认弱口令 + 非回环绑定的组合无人拦
- **位置**：`controller/config.py:34,36`
- **问题**：`API_TOKEN` 默认 `changeme`、`DASH_PASS` 默认 `wnidia2026`，
  而默认 `WNIDIA_HOST=0.0.0.0`——**照默认值部署就是"公网 + 默认口令"**，
  手册 8.2-9 明确禁止。
- **修复**：新增启动时合规自检 `compliance.preflight()`：非回环绑定 + 弱口令
  （长度 <12 / 常见弱口令 / 字符种类 ≤3）→ 抛 `ComplianceError`，**服务不启动**。
  仅当绑回环（本地演示）才豁免。部署脚本在未注入凭据时自动生成随机串。

### BUG-12（合规）只有 8888/9000 该绑 `0.0.0.0`，但没人管其它服务
- **位置**：`deploy_gx10.sh`（Agent 绑 `0.0.0.0:7000`）、`serve_jev.py`（`0.0.0.0` 且无鉴权）
- **修复**：新增 `compliance.assert_bind_allowed()`——绑 `0.0.0.0` 且端口不在
  `(8888, 9000)` 就抛错；`worker._bind_check()` 在 worker 启动时做同样检查；
  部署脚本把 Agent 改为只绑回环。

---

## 第二轮：本地全链路冒烟

### BUG-13（严重，测试与实现脱节）e2e/对抗测试的网关调用没带 Token
- **位置**：`tests/e2e_test.py` 的 `gateway()`、`tests/adversarial_test.py` 的 5 处
  `S.post(.../v1/chat/completions)` 与 `submit_spot()`
- **问题**：上一轮给 `/v1/chat/completions` 加了鉴权，但测试没同步。运行结果是
  必然 401：`e2e_test` 在取 `r.json()['choices']` 时抛 `KeyError` 直接崩；
  对抗测试更糟——`submit_spot()` 拿到 401 就立刻返回，`newest_spot()` 找不到任务，
  循环 `continue`，于是"抢占/恢复 8 轮无挂死"**变成一条永远为真的假通过**。
- **修复**：所有网关调用统一带 `Authorization`；并新增一条独立断言
  "无 Token 被拒 401"，把鉴权本身也纳入回归。

### BUG-14（韧性）任务会永久卡在 `binding`
- **位置**：`controller/harness.py`
- **问题**：`poll_running()` 只扫 `running`。一旦 `/start` 之后 worker 无响应、
  或 `inject_preemption()` 里 `_worker('/start')` 抛异常，任务就停在 `binding`
  且**没有任何回收机制**——看板上永远一条"绑定中"。
- **修复**：新增 `reclaim_stuck_binding()`，`bound_at` 超过
  `BINDING_TIMEOUT_S`（默认 60s）即重排队或判失败；接入 `_tick()`。

### BUG-15（韧性）抢占失败不回滚，被抢占的 spot 永不恢复
- **位置**：`controller/harness.py::inject_preemption()`
- **问题**：原实现先 `scheduler.preempt(spot)` + 登记 `RESUME_AFTER`，再启动高优任务；
  若启动失败就 `return False` 走人 → **高优任务卡在 binding，spot 已预占却永远不会被
  resume**（因为恢复逻辑挂在"高优任务完成"上，而它永远不会完成）。
- **修复**：显式回滚——撤销 `RESUME_AFTER` 登记、`/resume` 恢复 spot 并回写
  `running`、高优任务退回 `queued` 并累计 attempts，同时写 `rollback` 事件。

### BUG-16（韧性）启动失败无重试上限，任务无限重排
- **位置**：`launch_queued()`
- **问题**：异常分支只做 `attempts += 1`，从不检查 `MAX_ATTEMPTS`，
  于是 worker 长期不可用时该任务每 2 秒重排一次，永远停在 `queued`。
  另外 `r = _worker(...)` 的返回值被丢弃，**worker 明确返回 `ok=false` 也会被当成启动成功**。
- **修复**：解析响应体，`ok != true` 视为失败；失败累计到 `MAX_ATTEMPTS` 判 `failed` 并留事件。

### BUG-17（演示可靠性）掉的节点 5 秒就自己复活
- **位置**：`controller/registry.py::heartbeat()`
- **问题**：worker 心跳间隔 5s，`heartbeat()` 无条件把状态写回 `online`。
  `/admin/node/lost` 注入的"掉线"在 5 秒内被抹平，重调度与恢复这条演示路径
  评审根本看不清。
- **修复**：引入**隔离期**（默认 15s）：`mark_status(LOST)` 时写入 quarantine，
  隔离期内心跳只更新指标、不改状态，到期后由心跳自然恢复。对抗测试新增
  "隔离期内粘住 + 隔离期后恢复"两条断言固化该行为。

### BUG-18（前端）`summarize()` 读错键，"下一步"提示永不显示
- **位置**：`agent/app.py`（读 `data['next']`）vs `skills/idle-onboarding/tool.py`（返回 `next_steps`）
- **修复**：按 `next_steps` 读取，并兼容旧键；同时给 `blocked_by_compliance` 分支
  加了专门的解读文案。

### BUG-19（观察性）看板看不到"用的哪个引擎""有没有降级"
- **位置**：`worker/agent.py` 心跳、`controller/main.py` `/admin/state`
- **修复**：心跳新增 `engine / engine_healthy / degraded`；`nodes` 表加三列
  （走 BUG-5 的迁移）；`/admin/state` 与新增的 `/admin/engines`、`/admin/compliance`
  端点把引擎矩阵与合规状态暴露出来；`resource-doctor` 新增
  `engine_unhealthy` / `engine_degraded` 两个诊断码。

---

## 第三轮：边界 / 对抗 / 合规

### BUG-20（语义，逻辑错误）"要求可复现"时会选中不产出 token 的 mock
- **位置**：`controller/engines.py::score()`（初版打分）
- **现象**：`es-degrade-when-cuda-path-unavailable` 用例期望降级到 `ollama`，
  实际选中了 `mock`。
- **根因**：mock 的 `exact=True`（确定性函数确实可复现），在"要求可复现"的场景里
  它拿到 +40，而真实但非 exact 的 ollama 拿 −25，于是 mock 反超。
  **结论荒谬：为了"可复现"而选了一个不产出真实 token 的引擎。**
- **修复**：新增 `real_available` 参数——**只要有一台真实引擎探活通过，
  mock 额外 −45 分**；同时把这条约束同步到 Skill 侧的打分实现（两处保持一致）。

### BUG-21（可用性）CORS 全放开
- **位置**：`controller/main.py:18` `allow_origins=['*'], allow_methods=['*'], allow_headers=['*']`
- **问题**：公网端口 9000 上的 API 允许任意来源带任意头跨域调用，放大 CSRF 面。
- **修复**：默认只允许同源（`127.0.0.1:8888` / `localhost:8888`），
  需要跨域时通过 `WNIDIA_CORS_ORIGINS` 显式列白名单；方法收紧为 `GET, POST`。

### BUG-22（鲁棒性）`httpcli.request()` 在 `retries=0` 时 `raise None`
- **位置**：`controller/httpcli.py:38`
- **问题**：循环体不执行时 `last` 仍为 `None`，`raise last` 抛 `TypeError`，
  掩盖真实原因。
- **修复**：`last` 预置为 `EmptyResponse('no attempt made')`，保证 `raise` 一定有对象。

### BUG-23（健壮性）评测运行器的解释器路径与部署环境不一致
- **位置**：`tests/e2e_test.py` / `adversarial_test.py` 启动 `run_local.sh` 时用的是 PATH 里的 `python3`
- **修复**：`run_local.sh` 支持 `WNIDIA_PY` 覆盖；测试通过
  `env.setdefault('WNIDIA_PY', sys.executable)` 把"当前解释器"传下去，
  保证"跑测试的人"和"跑服务的人"是同一个 Python。

### BUG-24（合规）引擎接入的前置条件没写进代码
- **位置**：`scripts/deploy_gx10.sh`（新增 `ENGINE=tensorfold` 分支）
- **问题**：TensorFold 的 CUDA 路径依赖 NGC 容器；本机 `docker info` 里没有 nvidia runtime
  时，脚本会"照常往下跑"，起一个永远不健康的引擎，演示时才发现。
- **修复**：`ENGINE=tensorfold` 时先探测 nvidia runtime，缺失则打印
  **"手册 8.1-5 允许 conda/容器隔离，但 `--gpus all` 依赖 nvidia-container-toolkit；
  补齐属系统级变更需先向组委会确认"** 并 `exit 2`，绝不静默装死。

### BUG-25（测试基础设施）测试之间互相污染：残留实例 → 401 → 误导性 KeyError
- **位置**：`tests/e2e_test.py` / `tests/adversarial_test.py` 的 `finally` 清理块
- **现象**：连续跑 `e2e_test` → `adversarial_test` 时，第二条用例在
  `newest_spot()` 抛 `KeyError: 'tasks'`。查看端口，**9 个上次的进程仍在监听** 9000/8888/8101-8103。
- **根因（两层）**：
  1. `os.killpg(os.getpgid(proc.pid), SIGTERM)` 后 `run_local.sh` 的 trap 会让 bash 先退出，
     1 秒后再执行 `os.getpgid(proc.pid)` 时 pid 已被回收 → 抛 `ProcessLookupError`
     → 被 `except` 吞掉 → **SIGKILL 从未发出**，uvicorn/worker 子进程存活。
  2. 新实例启动时端口被旧实例占用（或竞争），新 Token 打到旧实例得到 401，
     `state()['tasks']` 便抛出一个与真实原因毫无关系的 `KeyError`，排查方向被完全带偏。
- **修复**：
  1. 新增 `tests/_clean.py`：`pgid_of()` 在进程启动后**立刻**记下进程组（之后 pid 可能被回收）；
     `kill_tree()` 保证 SIGKILL 一定会发出；`free_ports()` 只清理**本项目**的残留进程，
     不碰用户其他服务；`preflight_ports()` 在测试开始时确保端口可用。
  2. 两个测试的 `state()` 改为显式检查状态码，非 200 时抛出**可操作**的报错
     （指出"端口被旧实例占用"并给出 `lsof -ti:9000 | xargs kill -9`），
     不再让 `KeyError` 掩盖真因。

### BUG-26（同一条链上的第三个坑）识别残留进程的两版错误做法
- **现象**：修好 BUG-25 后，清场逻辑"什么都没清理"，反而报"端口被非本项目进程占用"，
  明明 `lsof` 显示那 6 个 PID 全是 `python3.13` 且正好监听 9000/8888/7000/8101-8103。
- **根因（两次踩坑）**：
  1. 第一版用 `ps -o command= -p <pid>` 拿命令行做特征匹配 —— 在某些沙箱/受限环境里
     `ps` 返回 `operation not permitted`，函数拿到空字符串，**匹配必然失败**。
  2. 第二版改用 `lsof -Fpc` 解析，字段里**没有端口号**（`-F` 只输出显式列出的字段），
     解析结果恒为空表。
- **修复**：`lsof -nP -Fpcn`（补上 `n` 字段）取 pid/命令/端口；并且**必须同时满足**
  「命令以 `python` 开头」与「端口指纹匹配本项目」才动手清理——
  指纹按端口逐个校验（9000 返回 `{"ok":true}`、7000 返回含 `skills`、
  8888 返回 401 + `WWW-Authenticate: Basic`、8101-8103 返回含 `node`/`gpu`）。
  任一不满足就**不动它**，只打印可操作提示。
- **教训**：清理动作宁可漏杀不可错杀；而"识别"这一步必须**自证有效**
  （`tests/_clean.py` 可直接单独运行，打印每个端口的 pid/命令/是否本项目）。

---

## 汇总

| 轮次 | 发现 | 其中高危 |
|---|---|---|
| 第 0 轮（评测地基） | 4 个断言键未实现 + 3 项运行器缺陷 | 1（假绿） |
| 第一轮（静态） | 12 | 4（扫描内网、明文凭据、默认弱口令+公网绑定、IP 错配） |
| 第二轮（冒烟） | 7 | 2（任务永久卡 binding、抢占不回滚） |
| 第三轮（对抗/合规） | 5 | 1（选路语义错误） |
| 第四轮（测试基础设施） | 3（BUG-25 含 2 个根因 + BUG-26 含 2 个根因） | 2（测试互相污染并掩盖真因） |
| **合计** | **31** | **10** |

### 仍有边界未覆盖（如实标注）

1. **真机并发与显存竞争**：本地为 mock，未覆盖真实 GPU 显存超分、MPS 抢占、
   多引擎共驻 128 GB 统一内存的表现；
2. **JEV live 精度**：默认 `mock` 占位，真实 openJev 权重未在本机加载，
   中文场景的语义判定精度需真机标定；
3. **TensorFold CUDA 路径**：无 nvidia runtime，未实测；接入步骤与前置条件已文档化
   （`docs/ENGINE-ADAPTER.md`），但"能不能跑"必须到真机验证；
4. **多机（双 Spark `--tp 2`）**：未验证 NCCL 链路与 rendezvous；
5. **多租户隔离**：仍为演示级（Token + 配额），生产级五层隔离未实现。
