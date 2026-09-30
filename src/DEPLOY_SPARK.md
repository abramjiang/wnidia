# DGX Spark 云部署指南（WNIDIA）

本指南面向第三届 NVIDIA DGX Spark Hackathon 分配的 **DGX Spark / ASUS Ascent GX10** 节点，指导把 WNIDIA 部署为可通过公网访问的服务。

> 若你的环境暂时没有 GPU，可先用 `bash scripts/run_local.sh` 在任意机器完成全链路演示（mock）。

## 1. 节点访问信息（赛事统一）

- 公网跳板：`<PUBLIC_IP_2>`，登录用户 `Developer`（非 root）；
- SSH 端口：`6NN`，其中 `NN` 为节点号（51–100）；
- 内网网段：`<LAN_SUBNET>/24`，节点地址 `192.168.110.NN`。

登录：

```bash
ssh -p 6NN Developer@<PUBLIC_IP_2>
```

## 2. 端口映射规则（重要）

| 节点端口 | 公网端口 | 用途 | 绑定要求 |
|---|---|---|---|
| `8888` | `8NN` | WNIDIA 看板 | **必须 `0.0.0.0`** + Basic 鉴权 |
| `9000` | `9NN` | WNIDIA API / OpenAI 兼容网关 | **必须 `0.0.0.0`** + Bearer Token |
| `7000` | — | Agent 应用层 | **只绑 `127.0.0.1`**，走 SSH 隧道 |
| `8101-8103` | — | worker 控制接口 | **只绑 `127.0.0.1`**（无鉴权，绝不外露） |
| `11434 / 8101 / 8080` | — | 推理引擎 | **只绑 `127.0.0.1`**，由控制面统一代理 |
| `8201` | — | JEV 决策服务 | **只绑 `127.0.0.1`** |

- 组委会**只为 8888/9000 做了公网映射**（手册 4.1）。其余端口绑 `0.0.0.0` 不但穿不出去，
  还会把服务暴露给同网段的其他 49 支队伍——所以本项目只允许 8888/9000 绑 `0.0.0.0`，
  这条约束由 `controller/compliance.py` 在代码层强制，违反即拒绝启动。
- 访问非映射端口用 SSH 隧道（手册第五章）：
  `ssh -p 6NN -L 7000:localhost:7000 -L 8080:localhost:8080 Developer@<PUBLIC_IP_2>`
- `8888/9000` **必须有鉴权**。本项目已取消 `changeme` / `wnidia2026` 这类默认口令：
  未显式注入时会**自动生成随机串**，且终端只回显掩码。

## 3. 上传代码（单次 scp < 1GB）

在本地（包的上一级目录）打包并上传：

```bash
# 本地：先清理测试残留（库、日志、缓存），包体积约 210 KB，远低于 1 GB 限制
cd wnidia_v3 && rm -rf data/wnidia.db* data/runlogs .venv
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
cd .. && zip -qr wnidia_v3.zip wnidia_v3 -x '*/__pycache__/*' '*.pyc'
scp -P 6NN wnidia_v3.zip Developer@<PUBLIC_IP_2>:~/
```

节点上解压：

```bash
unzip -o wnidia_v3.zip && cd wnidia_v3
```

## 4. 部署前确认

- 容器运行时与 NVIDIA Container Toolkit：

  ```bash
  docker info | grep -i runtime        # 期望看到 nvidia
  nvidia-smi                           # 确认 GPU 与显存（GB10 约 128GB 统一内存）
  ```

- 公共权重（如赛事提供）：常见只读目录 `/home/xsuper/models/`。确认模型路径后用环境变量覆盖：

  ```bash
  ls /home/xsuper/models
  export MODELS_DIR=/home/xsuper/models
  export MODEL_PATH=/models/<你的模型目录>
  export VLLM_IMAGE=<节点可用的 vLLM 镜像，如 vllm/vllm-openai:latest>
  ```

## 5. 一键部署

### 5.0 推荐路径：GX10 一键脚本（tmux 托管，含合规自检）

```bash
NODE_NUM=<51-100> \
WNIDIA_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))") \
WNIDIA_DASH_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))") \
MODE=gpu ENGINE=ollama bash scripts/deploy_gx10.sh
# 引擎可选 ollama | vllm | tensorfold（tensorfold 需要 nvidia runtime，缺失会 exit 2）
```

脚本会：合规前置检查（凭据强度/公网端口换算）→ 引擎就绪检查 → 清理旧进程 →
依赖准备 → `tmux` 会话 `wnidia`（窗口 api/dash/agent/cloud/edge/cpu）→ 健康检查 →
打印引擎选路与端口绑定自检。

### 5.1 容器路径：docker compose

```bash
export WNIDIA_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
export WNIDIA_DASH_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
bash scripts/deploy_spark.sh
```

脚本会：可选启用 MPS（失败自动回退多实例）→ 构建镜像 → 启动控制器与多个 vLLM 引擎/worker → 健康检查。

### 5.2 （可选）启用真实 Jev 决策增强（开源模型）

默认 `WNIDIA_JEV_MODE=mock`（离线确定性占位，不代表真机精度）。决策模型已由 TypeSafe 商业 `typesafe/jev-1.13` 替换为开源 `heman10x/openJev-verdict-2.0`（151M ModernBERT，非自回归，RLCD 校准）。三种接法：

**A. 自建 http 服务（推荐）**：先 `pip install -r requirements-jev.txt` 再起 `python scripts/serve_jev.py`，控制器走 Jev 兼容 `/v1/systemone`：

```bash
export WNIDIA_JEV_MODE=live
export WNIDIA_JEV_BACKEND=http
export WNIDIA_JEV_BASE=http://127.0.0.1:8201        # Docker 内改容器 DNS
export WNIDIA_JEV_MODEL=heman10x/openJev-verdict-2.0
```

**B. 进程内 local**：不额外起服务，首次调用懒加载 torch/transformers：

```bash
export WNIDIA_JEV_MODE=auto                          # local 后端无需密钥自动 live
export WNIDIA_JEV_BACKEND=local
export WNIDIA_JEV_MODEL=heman10x/openJev-verdict-2.0
```

**C. Laya / vLLM**：`JEV_BASE` 指向 vLLM `structured_server` 的 `/v1/systemone`，其余同 A。

真实模型在中文（CJK）场景不信任自动惩罚、回退精确匹配；端点不可达 / 超时 / 无密钥 / 缺权重自动降级，主流程与确定性版本一致。运行状态见看板或 `/admin/state` 的 `jev` 字段。

GPU 切分（可按 `gpu-slicer` 输出调整 `deploy/docker-compose.gpu.yml`）：

- `engine-0`：`gpu_memory_utilization 0.45`（主力/prefill）；
- `engine-1`：`gpu_memory_utilization 0.15`（decode/边缘）；
- 预留约 8%，合计不超过显存上限。

## 6. 验证

```bash
TOK="$WNIDIA_TOKEN"

# 本机健康
curl -fsS http://127.0.0.1:9000/healthz

# ① 鉴权必须生效（不带 Token 应 401）
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}]}'      # 期望 401

# ② 带 Token 的正常问答
curl -fsS -X POST http://127.0.0.1:9000/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOK" \
  -d '{"messages":[{"role":"user","content":"你好"}]}'

# ③ 引擎可插拔层视图（目录 + 实时探活 + 选路建议）
curl -fsS -H "Authorization: Bearer $TOK" \
  'http://127.0.0.1:9000/admin/engines?probe=true' | python3 -m json.tool

# ④ 合规自检视图（对手册第八章，答辩可直接投屏）
curl -fsS -H "Authorization: Bearer $TOK" \
  http://127.0.0.1:9000/admin/compliance | python3 -m json.tool

# ⑤ 端口绑定自检（手册 4.2 的做法）：8888/9000 为 0.0.0.0，其余必须全是 127.0.0.1
ss -tlnp | grep -E ':(8888|9000|7000|8101|8102|8103)\b'

# ⑥ 内网探测必须被拒绝（手册 8.1-2），且不会发出任何请求
python skills/idle-onboarding/tool.py --node x \
  --worker-host <LAN_IP> --dry-run | grep blocked_by_compliance

# 公网（用 9NN 端口）
curl -fsS -H "Authorization: Bearer $TOK" http://<PUBLIC_IP_2>:9NN/healthz
```

打开浏览器访问看板：`http://<PUBLIC_IP_2>:8NN`（Basic 鉴权）。
Agent 只绑回环：先建隧道 `ssh -p 6NN -L 7000:localhost:7000 Developer@<PUBLIC_IP_2>`，
再访问 `http://localhost:7000/?token=<Token>`。

## 7. 同口径压测

```bash
# after（经 WNIDIA）
python bench/load.py --target http://127.0.0.1:9000 \
  --token "$WNIDIA_TOKEN" --requests 60 --concurrency 8

# before（直连单个 vLLM，替换为引擎地址/端口）
python bench/load.py --target http://127.0.0.1:8200 \
  --token none --requests 60 --concurrency 8
# 或一键对比
python bench/load.py --compare \
  --baseline-url http://127.0.0.1:8200 \
  --wnidia-url http://127.0.0.1:9000 --token "$WNIDIA_TOKEN" \
  --requests 60 --concurrency 8
```

真机请同时采集三类证据：DCGM/Nsight（利用率）、GenAI-Perf（推理性能）、LM-Eval（质量），并产出 `BENCHMARK.md`。

## 8. 运维

```bash
docker compose -f deploy/docker-compose.gpu.yml logs -f controller
docker compose -f deploy/docker-compose.gpu.yml restart cloud-0
docker compose -f deploy/docker-compose.gpu.yml down
```

长任务请在 `tmux` 中执行，避免 SSH 断开中断。

## 9. 赛事红线（务必遵守）

- 不改驱动 / SSH / 防火墙 / 权限，不刷固件 / BIOS，**不 reboot**；
- 不扫描、探测、登录其他节点（有日志）；
- 共享出口，`scp` 单次 < 1GB，磁盘保持 ≥20% 空闲；
- 不开放无鉴权 Web 终端。

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| 公网访问不通 | 确认服务绑 `0.0.0.0`；确认使用公网端口 `8NN/9NN` |
| 引擎起不来 | 检查 `nvidia` runtime、模型路径、显存是否超分；降低 `gpu_memory_utilization` |
| MPS 异常 | `bash deploy/mps-off.sh`，自动回退多实例（核心不受影响） |
| 节点掉线 | 看板可见，任务自动重路由；`docker compose ... restart` 对应 worker |
| 磁盘紧张 | 清理镜像/日志，保持 ≥20% 空闲 |
