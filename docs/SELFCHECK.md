# 安全自查与 bug 自查工具

本目录把两项原本靠人工完成的工作固化为脚本：**脱敏校验** 与 **三层 bug 自查 + 核心功能保护**。

| 脚本 | 作用 |
|---|---|
| `scripts/sanitize_check.py` | 敏感信息扫描（公网 IP / 内网 IP / 密钥 / 私钥 / 口令 / 邮箱），本地目录与 GitHub 仓库双模式，支持 `--fix` 自动替换 |
| `scripts/selfcheck.py` | 三层 bug 自查（L1 语法 / L2 配置 / L3 运行时）+ 核心功能保护断言 |

---

## 一、脱敏校验器 `sanitize_check.py`

### 设计原则

**工具内不硬编码任何真实敏感串**——否则工具自身就成了泄漏源。采用**模式识别**：

- 公网 IPv4：按模式匹配，自动排除回环（`127.*`）、私有段（`10.*` / `192.168.*` / `172.16-31.*`）、未指定（`0.0.0.0`）与文档示例段
- 密钥：`gh[pousr]_*`、`AKIA*`、`sk-*`、私钥头、`PASSWORD/TOKEN=xxx` 形式
- 邮箱：排除常见占位符与 `example.com`
- 内网 IP：默认仅提示，`--strict` 时才判失败

环境特有的字面量（SSH 用户名、非标准端口、主机名等）通过**外部规则文件**传入：

```bash
python3 scripts/sanitize_check.py --dir . --rules sanitize_rules.json
```

规则文件格式（**该文件不应提交，建议加进 .gitignore**）：

```json
{
  "SSH用户名": ["你的用户名"],
  "SSH端口": ["你的非标准端口"],
  "主机密码": ["你的密码"]
}
```

> **关键教训**：脱敏**不能只换 IP**。曾经只替换了 IP，结果形如
> `ssh -p <端口> <用户名>@<PUBLIC_IP>` 的命令里，用户名与非标准端口仍然暴露了环境。
> 这类值请放进外部规则文件一并扫描。

### 用法

```bash
python3 scripts/sanitize_check.py --dir .                  # 本地扫描
python3 scripts/sanitize_check.py --dir . --fix            # 自动替换为占位符
python3 scripts/sanitize_check.py --dir . --strict         # 内网 IP 也判失败
GH_TOKEN=xxx python3 scripts/sanitize_check.py --repo owner/name
python3 scripts/sanitize_check.py --dir . --json           # CI 友好
```

---

## 二、三层 bug 自查 `selfcheck.py`

### 用法

```bash
python3 scripts/selfcheck.py                    # 全量 L1+L2+L3（本机）
python3 scripts/selfcheck.py --skip-runtime     # 只跑 L1+L2（CI 推荐）
python3 scripts/selfcheck.py --base http://127.0.0.1:9000
WNIDIA_TOKEN=xxx python3 scripts/selfcheck.py
```

### 三层内容

| 层 | 检查项 |
|---|---|
| **L1 语法与结构** | 全部 `.py` 通过 `py_compile`；`.js`（若有）通过 `node --check`；10 个核心文件齐备 |
| **L2 逻辑与配置** | `JEV_MODE` 合法（off/mock/live/auto）；`JEV_BACKEND` 合法（http/local/multi）；`.gitignore` 覆盖运行时产物（`*.log` / `*.db-wal` / `*.db-shm` / `__pycache__`）；调用 `sanitize_check` 确认无泄漏 |
| **L3 运行时端到端** | `/healthz`；`/admin/state` 节点在线；`/admin/demo/scenes` 非空（**防 P0 双重解包 bug**）；触发 `demo/run` 后步骤推进（**防步骤不亮**）；`demo/stop` 后同步停止（**防关闭不同步**）；`/admin/jev/report` 可用且留痕已落盘（**防 JEV 死界面**）；`/admin/metering` 可用；门户含「实时后端」「性能前端」（**防改名回退**） |

### 核心功能保护（FAIL 即视为能力回退）

- 调度与引擎选择（节点在线、引擎字段）
- 场景与步骤推进
- 可信裁决 JEV（报告可用 + 留痕落盘）
- 计量结算（`/admin/metering`）

### 安全护栏

默认**只允许探测 `127.0.0.1` / `localhost`**。目标非本机时跳过 L3 并提示：

```
⏭️ 运行时检查   SKIP  目标主机 X 非本机，已跳过（防误连参赛节点）。需探测请加 --allow-remote
```

用于避免误对**已截止部署的参赛节点**发起请求。

---

## 三、注入方式

### 1) CI 自动执行（GitHub Actions）

已提供 `.github/workflows/selfcheck.yml`，push / PR 时自动运行：

- 脱敏校验（本地目录模式）
- `selfcheck.py --skip-runtime`（L1+L2）

任一失败即阻断。

### 2) 本地提交前（pre-commit）

```bash
cat > .git/hooks/pre-commit <<'EOF'
#!/bin/sh
python3 scripts/sanitize_check.py --dir . || exit 1
python3 scripts/selfcheck.py --skip-runtime || exit 1
EOF
chmod +x .git/hooks/pre-commit
```

### 3) 部署后验收

```bash
MODE=preview bash scripts/deploy_gx10.sh
python3 scripts/selfcheck.py            # L1+L2+L3 全量
```

---

## 四、验收标准

| 场景 | 期望 |
|---|---|
| `sanitize_check.py --dir .` | 输出「干净 CLEAN」，退出码 0 |
| `selfcheck.py --skip-runtime` | 全部 PASS，退出码 0 |
| `selfcheck.py`（服务已起） | L3 核心项全部 PASS；SKIP 仅出现在未安装 node 或无服务时 |
