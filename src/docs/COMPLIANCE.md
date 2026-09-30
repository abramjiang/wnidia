# 合规对照表：手册第八章红线 → 代码与部署动作（v4）

> 依据：《Spark 云节点访问与使用手册》第八章「注意事项与使用红线」。
> 原则：**默认拒绝（fail-closed）**——不提供"先跑起来再说"的路径。
> 代码入口：`controller/compliance.py`；自检视图：`GET /admin/compliance`。
>
> v4 更新：新增能力（断网自治回放、批量入队注入、租户门户、QPU/机队接口）
> 全部纳入下表与「出站连接清单」；§8.5 记录 **v5 对「推理数据留本地」的落地**。

## 8.1 绝对禁止（触碰即回收节点）

| # | 手册条款 | 代码/部署动作 | 位置 |
|---|---|---|---|
| 1 | 严禁加密货币挖矿、网络攻击（DDoS、**端口扫描**、暴力破解、漏洞利用）、翻墙代理、木马、违禁内容、爬取倒卖数据 | 项目不含任何扫描器/爆破/代理组件；全仓仅对**已知目标**发起 HTTP（worker、引擎、控制面）；审计见下表「全仓出站连接清单」 | 全仓 |
| 2 | 严禁扫描、探测、入侵内网其他节点（`<LAN_SUBNET>/24`）或跳板机 | `compliance.assert_probe_allowed()`：显式封禁 `<LAN_SUBNET>/24`，非回环目标必须写入 `WNIDIA_ALLOW_HOSTS` 白名单；`idle-onboarding` 在探活**之前**校验，未通过则**不发起任何请求**并返回结构化错误 | `compliance.py`、`skills/idle-onboarding/tool.py:check_target` |
| 3 | 严禁商业用途/转租转售节点；严禁分享账号密码或公开发布 | 部署脚本**不再回显明文凭据**，只打印掩码（`mask()`）；`env.sh` 权限收紧为 `600`；文档与 Skill 输出不含任何口令 | `compliance.mask`、`deploy_gx10.sh`、`deploy_spark.sh` |
| 4 | 严禁修改系统级配置（登录密码、sshd_config、防火墙/iptables、网络路由、用户权限） | 项目不写任何系统配置；无 `iptables`/`sshd_config`/`passwd` 操作；`tensorfold` 若需 nvidia-container-toolkit，脚本会 **exit 2 并提示先向组委会确认** | `deploy_gx10.sh` |
| 5 | 严禁卸载/降级 GPU 驱动、刷固件、改 BIOS；需特定 CUDA 版本请用 conda 或容器隔离 | 引擎运行在容器或 venv 内，不触碰宿主驱动；`ENGINE=tensorfold` 前置检查 nvidia runtime，缺失即拒绝启动 | `deploy_gx10.sh` |
| 6 | 严禁 `reboot` / `shutdown` / `poweroff` | 全仓无这些命令；停止服务只杀自己的进程 / `tmux kill-session` | 全仓 |

## 8.2 严格限制

| # | 手册条款 | 代码/部署动作 | 位置 |
|---|---|---|---|
| 7 | 单次 >1 GB 严禁 `scp`；改为节点内下载或 `rsync -P` | 文档明确要求模型权重（如 TensorFold 的 16.1 GB + 3.8 GB）**在节点内**用 `tensorfold pull` / `modelscope download` 拉取；本项目自身代码包仅 ~260 KB，`scp` 无压力 | `docs/ENGINE-ADAPTER.md`、`DEPLOY_SPARK.md` |
| 8 | 磁盘保持 ≥20% 空闲 | 部署脚本先建 `data/`；默认**不再自动删库**（`RESET=1` 才清），避免误删与重复下载；README 提示清理 `~/.cache/huggingface`、`~/.cache/pip` | `deploy_gx10.sh` |
| 9 | 暴露在 8888/9000 上的服务必须设 token 或密码；严禁无鉴权的文件管理器/Web 终端/数据库控制台挂公网 | ① `compliance.assert_bind_allowed()`：**只有 8888/9000 允许绑 `0.0.0.0`**；② 非回环绑定 + 弱口令 → **启动即失败**（`assert_secret_strength`）；③ `/v1/chat/completions`、`/admin/*`（含 v4 新增的 `/admin/inject/queue`、`/admin/settlement`、`/admin/trust/*`、`/admin/audit/*`、`/admin/gates`、`/admin/offline`、`/admin/qpu/*`、`/admin/fleet/*`、`/v1/tenant/{t}/usage`）与 `/internal/*`（含 `/internal/offline/batch`）**全量 Bearer 鉴权**，看板全量 Basic 鉴权；④ worker / Agent / 引擎一律只绑回环；⑤ `/portal` 是**静态外壳**——页面内不含任何数据、不含 Token，数据全部由用户在页面内粘贴 Token 后走上面的鉴权接口 | `compliance.py`、`main.py@startup`、`worker._bind_check`、`deploy_gx10.sh` |
| 10 | 严禁存储个人隐私/公司内部/涉密/未授权第三方数据 | 数据目录只有 SQLite；**v5 起 prompt 默认不落库明文**（只留占位标记 + 摘要 + 长度），调度所需副本只在控制面进程内存、任务终态即释放；所有对外出口（`/admin/state`、看板 `/api/state`、租户用量、证据包）字段级脱敏；含明文的临时文件走 0600 + 用完即删；`data/` 与 `data/tmp/` 均在 `.dockerignore` 内。落地细节与剩余边界见 §8.5 | `prompt_guard.py`、`db.py`、`dash.py`、`agent/app.py`、`.dockerignore` |
| 11 | 长任务用 tmux/screen 托管 | 部署脚本所有常驻进程一律 `tmux` 会话 `wnidia`（窗口 `api/dash/agent/cloud/edge/cpu`） | `deploy_gx10.sh` |

## 8.3 数据安全与备份

| # | 手册条款 | 应对 |
|---|---|---|
| 12 | 节点不提供备份，无快照 | 代码包在本地留档；`docs/BUG-HUNT-3ROUNDS.md`（v4）、`docs/BUG-HUNT-3ROUNDS-v3.md`（历史）与本文档随代码一起带走 |
| 13 | 代码及时 push 到自己的 Git，权重/结果定期导出 | 建议 `git init` 后 push；`data/wnidia.db` 即实验证据，可用 `/admin/state` 导出 JSON，或用 `/admin/audit/export` 导出**可校验的证据包** |
| 14 | 比赛结束后节点统一回收清空 | 提交前导出：`/admin/state` 全量 JSON、`data/sandbox_v4_result.json`、`BENCHMARK.md` 真机数据、演示录屏 |

## 8.4 使用礼仪

| # | 手册条款 | 应对 |
|---|---|---|
| 15 | 只使用分配给自己队伍的那台节点 | `idle-onboarding` 的默认 `--worker-host` 是 `127.0.0.1`；探活其他主机需显式白名单 |
| 16 | 提前完赛主动释放资源 | 部署脚本不注册任何开机自启/系统服务，`tmux kill-session -t wnidia` 即可彻底释放 |
| 17 | 故障联系技术支持，不要自行重启重装 | 文档与脚本均不含重启动作 |

## 8.5 推理数据（prompt）留存：已处理（v5）

| 项 | 状态 |
|---|---|
| 问题 | BP P13 决策③写「控制面/数据面分离、推理数据留本地」，但实现把 prompt 整段存进中心 SQLite，`/admin/state` 与看板 `/api/state` 都能读到全部历史 prompt |
| 处理 | **prompt 默认不落库明文**：库里只写占位标记 `[REDACTED:prompt-not-stored]` + `prompt_digest` + `prompt_chars`；调度所需的那份只活在**控制面进程内存**，任务终态即释放；所有对外出口（`/admin/state`、看板 `/api/state`、`/v1/tenant/{t}/usage`、审计证据包）统一字段级脱敏 |
| 开关 | `WNIDIA_STORE_PROMPT=0`（默认关）/ `WNIDIA_REVEAL_PROMPT=0`（默认关）——**两个都开**才可能看到全文（「与」关系，单开关误翻不会泄露） |
| 老库 | 启动时把历史遗留明文一次性抹掉（`WNIDIA_SCRUB_LEGACY=1` 默认开） |
| 可核验 | 脱敏后仍给 `prompt_digest` 与 `prompt_chars`：能证明"用的是哪段输入、有多长"，但不泄露内容 |
| 独立报告 | `docs/PROMPT-PRIVACY.md`（设计、代价、两轮自查共 8 个问题，4 高危） |

**仍然存在的边界（必须讲清，不能含糊）**：

1. **prompt 会经网络发往承接任务的 worker 节点** —— 这是必需的（worker 要拿它推理）。
   因此"数据不出域"只在"计算节点都在自己边界内"时成立，答辩要主动说明。
2. **`JEV_MODE=live` 时 prompt 会发往 JEV 服务**（默认 `127.0.0.1:8201`）。
   默认是 `mock`（纯本地、不出网）。用 live 就必须承认这条。
3. **控制面进程重启后，未终态任务的 prompt 不可恢复** —— 这类任务会在派发前
   **显式判失败**（`error=prompt_unavailable`），而不是拿占位符跑出"看着正常其实是假"的结果。
   这是「数据不留中心」的必然成本。
4. `prompt_digest` 用于比对同一输入，**不承诺抗枚举**（极短输入理论上可被穷举）。

## 附一：全仓出站连接清单（供审计）

| 发起方 | 目标 | 目标来源 | 是否受合规校验 |
|---|---|---|---|
| `worker.agent` | `{CTRL}/internal/register`、`/internal/heartbeat` | 环境变量（默认 `127.0.0.1:9000`） | 是（注册前 `_bind_check`） |
| `worker.agent` | `{CTRL}/internal/offline/batch` ★v4 断网批次回放 | 环境变量（默认 `127.0.0.1:9000`） | 是（同 TCP 目标；仅回环默认值） |
| `worker.agent` | `{引擎}/chat/completions`、`{引擎}/healthz` | 环境变量 / `ENGINE_CATALOG` 默认值 | 是（`engines.probe_openai_compat`） |
| `controller.harness/executor` | `{node}/start|/status|/execute|/preempt|/resume` | 已注册节点的 `vllm_port`（来自节点主动注册） | 是（注册来源受鉴权与画像约束） |
| `controller.jevclient` | `{JEV_BASE}/v1/systemone`（**payload 含 prompt 明文**） | 环境变量 | 是（仅回环默认值）；**默认 `JEV_MODE=mock` 不出网**，切 live 即等于把 prompt 发给该服务，见 §8.5 边界② |
| `skills/resource-doctor`、`smart-dispatcher`、`gate-inspector` ★v4、`cc-auditor` ★v4、`engine-selector` | `{ctrl}/admin/*` | `--ctrl`（默认回环） | 是 |
| `skills/idle-onboarding` | `{worker_host}:{port}/healthz` | **用户输入** | **是（加固重点）** |
| `skills/edge-box-provisioner` ★v4 | `{ctrl}/admin/state`（仅 `--live` 时） | `--ctrl`（默认回环） | 是 |
| `skills/private-cloud-planner` ★v4 | **无网络调用**（纯离线测算） | — | 不适用 |
| `agent.app` → `skills/smart-dispatcher` | 本地子进程（含 prompt 的临时文件） | `data/tmp/task-*.json`（0600） | 不适用（无网络）；v5 起不再把 prompt 放进 argv，跑完即删 |
| `deploy_*.sh` | `curl 127.0.0.1:*` | 硬编码回环 | 是 |

**没有**任何 ping / nmap / ssh / 端口段扫描类调用。

## 附二：v4/v5 新增接口的鉴权与绑定核对表

| 接口 | 鉴权 | 绑定 | 备注 |
|---|---|---|---|
| `POST /admin/inject/queue` | Bearer | 跟随 API 绑定规则（仅 8888/9000 可公网） | 演示注入；对脏输入做收敛并回报 `sanitized` 字段 |
| `POST /internal/offline/batch` | Bearer | 同上 | 幂等（同 `batch_id` 只记一次） |
| `GET/POST /admin/settlement` | Bearer | 同上 | 同周期**先清后写**，不重复计账 |
| `GET/POST /admin/trust*`、`/admin/audit/*` | Bearer | 同上 | 证据包默认不含 prompt 明文 |
| `GET /admin/gates`、`/admin/offline`、`/admin/qpu`、`/admin/fleet` | Bearer | 同上 | — |
| `GET /v1/tenant/{tenant}/usage` | Bearer | 同上 | 只读 |
| `GET /portal` | **无**（静态外壳） | 同上 | 页面不含数据/Token；数据走上述鉴权接口 |
| `GET /admin/state?reveal_prompt=1` | Bearer | 同上 | 需 `STORE_PROMPT=1` **且** `REVEAL_PROMPT=1` 才返回全文，否则仍是预览 |
| `GET /api/state`（看板 :8888） | Basic | **公网映射端口** | v5 起与 `/admin/state` 同口径脱敏（B5-04 修复） |
| `GET /healthz` | **无** | 同上 | 仅返回 `{ok, version}`，无集群信息 |
| worker `/internal/*`（无鉴权的控制接口） | — | **强制回环**（`_bind_check` 启动即拒绝非回环） | 手册第五章做法 |

## 如何自证

```bash
# 1) 运行时合规视图（需 Token）
curl -s -H "Authorization: Bearer $WNIDIA_TOKEN" \
  http://127.0.0.1:9000/admin/compliance | python3 -m json.tool

# 2) 端口绑定自检（手册 4.2 的做法）
ss -tlnp | grep -E ":(8888|9000|7000|8101|8102|8103|8104)\b"
#   预期：8888 / 9000 为 0.0.0.0（或按本地调试用回环），其余全部 127.0.0.1

# 3) 内网探测必须被拒绝（对手册 8.1-2）
python skills/idle-onboarding/tool.py --node x --worker-host <LAN_IP> --dry-run
#   预期：blocked_by_compliance=true，且**没有发出任何请求**

# 4) 弱口令 + 公网绑定必须拒绝启动（对手册 8.2-9）
WNIDIA_HOST=0.0.0.0 WNIDIA_TOKEN=changeme python -m uvicorn controller.main:app --port 9000
#   预期：ComplianceError，服务不启动

# 5) 证据包默认不含 prompt 明文（v4）
curl -s -X POST -H "Authorization: Bearer $WNIDIA_TOKEN" \
  "http://127.0.0.1:9000/admin/audit/export?since_ms=0" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('prompts_included =', d['prompts_included'])"
#   预期：prompts_included = False

# 6) 一键跑通全部合规相关用例（v4 沙盒，含 A/B/C/G/J/K 等场景）
WNIDIA_PY=<python> python scripts/sandbox_v4.py --only ABGJK
```
