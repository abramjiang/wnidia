# WNIDIA v5.1 · gx10 节点部署手册（手动执行版）

> 适用：实际分配的 **gx10 节点（序号 76，主机名 gx10-53e8）**。
> SSH 登录与密码**由你本人手动完成**；部署脚本不索要、不读取、不保存 SSH 密码。
> 应用层 Token / 看板密码可由脚本自动生成强随机串，或你自行预置。

---

## 0. 节点信息（以分配表为准）

| 项目 | 值 |
| --- | --- |
| 序号 / 主机名 | 76 / **gx10-53e8** |
| 节点内网地址 | <LAN_IP_2> |
| 公网入口 IP | **<PUBLIC_IP>** |
| SSH 登录 | `ssh -p <SSH_PORT> <NODE_USER>@<PUBLIC_IP>` |
| SSH 用户 / 端口 | <NODE_USER> / **<SSH_PORT>** |

**公网端口映射（内网 → 公网），只有 3 个（2026-09-29 公网实测确认）：**

| 服务 | 内网端口 | 公网端口 | 鉴权 |
| --- | --- | --- | --- |
| Agent（自然语言 / 调度提议） | 7000 | **7026** | Bearer Token |
| 只读看板 | 8888 | **8026** | HTTP Basic |
| 控制面 API | 9000 | **9026** | Bearer Token |
| Ollama 推理（gpu 模式） | 11434 | 无（仅回环） | SSH 隧道 |
| worker（cloud/edge/cpu） | 8101/8102/8103 | 无（仅回环） | SSH 隧道 |

> 实测依据：公网 `:9026/healthz` 返回 `{"ok":true}`（API）；公网 `:8026/` 返回 401 HTTPBasic（看板）；公网 `:7026` 有映射（502=后端未起）；公网 `:8888` 超时（无映射）。
> 注意：本手册早期版本误把 API 推断为"内网 8026 / 公网 8026"、看板为"公网 8888"——按此部署会导致**公网 API 失联**（内网 8026 无任何公网映射）。已按实测修正。三个公网端口都有鉴权，符合"公网服务必须 token/密码"红线。

---

## 1. 上传部署包（本地电脑执行，手动输密码）

部署包体积很小（远小于 1GB 单次上限）。在**本地**终端：

```bash
# 进入部署包所在目录后：
scp -P <SSH_PORT> wnidia_v5.1_gx10.zip <NODE_USER>@<PUBLIC_IP>:~/
# 出现密码提示时，手动输入 SSH 密码（输入时不显示，属正常）
```

> 注意 `scp` 的端口参数是大写 `-P`；`ssh` 是小写 `-p`。

## 2. SSH 登录节点（手动输密码）

```bash
ssh -p <SSH_PORT> <NODE_USER>@<PUBLIC_IP>
```

登录成功后，以下命令都在节点上执行。

## 3. 解压

```bash
cd ~
unzip -o wnidia_v5.1_gx10.zip          # 解压出 ~/wnidia/
cd ~/wnidia
ls scripts/deploy_gx10.sh              # 能看到该文件即正确
```

## 4. 一键部署

```bash
# mock 模式（无 GPU 依赖，最快，建议先用它验证链路）
bash scripts/deploy_gx10.sh

# 真实 GPU 模式（Ollama 仅监听回环；需节点已装 ollama 并 pull 模型）
# MODE=gpu bash scripts/deploy_gx10.sh
```

脚本自动完成：生成/校验强凭据 → （gpu）启动回环 Ollama → 用户目录 venv 装依赖（无需 root）→ 写 600 权限 env → tmux 拉起 API/看板/Agent/3 个 worker → 健康检查。

**可选：预置应用凭据**（不预置则自动生成并在结尾提示你保存）

```bash
WNIDIA_TOKEN='你的强随机Token' WNIDIA_DASH_PASS='你的强随机看板密码' \
  bash scripts/deploy_gx10.sh
```

> 凭据要求：≥12 位、非纯数字/纯字母、非默认词。脚本会做强度校验，弱口令在公网端口上会被拒绝启动。

## 5. 验证

节点本机：

```bash
curl -s http://127.0.0.1:9000/healthz            # API：{"ok":true,...}
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8888/   # 期望 401（Basic）
curl -s http://127.0.0.1:9000/admin/compliance \
  -H "Authorization: Bearer <你的API Token>"      # 合规自检状态
```

公网（本地电脑浏览器 / curl）：

| 用途 | 地址 | 凭据 |
| --- | --- | --- |
| 看板 | http://<PUBLIC_IP>:8026 | 用户 reviewer / 看板密码 |
| API | http://<PUBLIC_IP>:9026 | Header `Authorization: Bearer <API Token>` |
| Agent | http://<PUBLIC_IP>:7026 | Bearer `<API Token>` |

API 调用示例：

```bash
curl -s http://<PUBLIC_IP>:9026/admin/state \
  -H "Authorization: Bearer <API Token>"
```

## 6. SSH 隧道（访问无公网映射的 Ollama / worker）

在**本地电脑**另开终端：

```bash
# Ollama
ssh -p <SSH_PORT> -L 11434:localhost:11434 <NODE_USER>@<PUBLIC_IP>
# worker（可叠加多个 -L）
ssh -p <SSH_PORT> -L 8101:localhost:8101 -L 8102:localhost:8102 -L 8103:localhost:8103 \
  <NODE_USER>@<PUBLIC_IP>
```

隧道建立后，本地访问 `http://127.0.0.1:11434` 即等于节点回环服务。

---

## 7. 日常运维

```bash
tmux attach -t wnidia          # 进入会话，窗口：api/dash/agent/cloud/edge/cpu
# 脱离：Ctrl+B 后按 D（不要 Ctrl+C，那会中断服务）
tmux kill-session -t wnidia    # 停止全部服务
ls data/runlogs/               # 日志目录（pip.log / 各窗口输出）
RESET=1 bash scripts/deploy_gx10.sh   # 清空旧库重新部署（慎用，清数据）
```

## 8. 赛事红线（务必遵守）

- 长任务一律 **tmux 托管**；SSH 断开后服务继续运行。
- **禁止** `reboot`、改系统配置（密码/SSH/防火墙/权限）、刷固件。
- **禁止扫描/探测内网其它节点**（合规守卫默认拒绝非回环探测）。
- `scp` 单次 < 1GB；磁盘保持 ≥20% 空闲。
- 公网服务必须 token/密码鉴权（本部署三个公网端口均已鉴权）。
- 不在节点存储隐私/涉密数据；凭据不回显、不截图外传。

## 9. 故障排查

| 现象 | 处理 |
| --- | --- |
| 依赖安装失败 | 看 `data/runlogs/pip.log`；确认 `python3 -m venv` 可用，必要时联系组委会 |
| API 未就绪 | `tmux attach -t wnidia` 看 api 窗口；多为端口占用或依赖问题 |
| 看板返回非 401/无法登录 | 核对用户 reviewer 与看板密码；密码在 `data/runlogs/env.sh`（600） |
| 公网打不开 | 确认用的是 7026/8026/9026；看板=**8026**、API=**9026**（公网 8888 无映射） |
| gpu 模式推理失败 | `curl http://127.0.0.1:11434/api/version`；确认已 `ollama pull <模型>` |
| 端口被旧进程占用 | `bash scripts/deploy_gx10.sh` 会自动清理；仍异常可 `pkill -f controller.main` 后重跑 |
