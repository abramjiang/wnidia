# 部署指南 · DEPLOY

本指南说明如何将 WNIDIA v5.1 部署到节点（GPU 模式）或本地预览（单机模式）。

---

## 1. 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux（推荐 Ubuntu 22.04+）/ macOS（预览） |
| Python | 3.9+（推荐 3.13） |
| Node | 18+ |
| 推理引擎 | Ollama（演示默认）；vLLM / TensorFold 可选 |
| GPU | 可选，演示环境为 GX10 节点 GPU |

---

## 2. 一键部署（节点 GPU 模式）

```bash
bash scripts/deploy_gx10.sh   # 内部 MODE=gpu，拉起 tmux 6 窗口
```

脚本执行流程：

1. **停止旧实例**：`tmux kill-session -t wnidia` + 清理残留进程。
2. **依赖继承**：复用已存在的 `.venv`，免联网重装。
3. **启动**：`MODE=gpu` 拉起 tmux 6 窗口 —— `api / dash / agent / cloud / edge / cpu`。
4. **引擎绑定**：Ollama 仅绑回环 `127.0.0.1:11434`（安全）。
5. **自检**：本机 + 公网端点冒烟验证。

---

## 3. 本地预览（单机 / Mac）

```bash
MODE=preview bash scripts/deploy_gx10.sh
```

预览端口（本机）：

| 服务 | 本机端口 |
|---|---|
| API | 9900 |
| 看板 / 门户 | 9888 |
| Worker | ×3 |

---

## 4. 端口与凭据

| 服务 | 本机端口 | 公网端口 | 访问方式 |
|---|---|---|---|
| API | 9000 | 9026 | `Authorization: Bearer previewtoken` |
| 看板 / 门户 | 8888（根路径 `/`） | 8026 | Basic `reviewer` / `previewpass` |
| Agent | 7000 | 7026 | 内部 |

> ⚠️ 安全提示：演示凭据为固定值，公网部署请务必替换为强凭据，并限制来源 IP。

---

## 5. 关键端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/healthz` | GET | 健康探针 |
| `/admin/jev/report` | GET | JEV 实测报告（需 Bearer） |
| `/admin/demo/run?scene=edge` | GET/POST | 触发场景剧本执行 |
| `/admin/demo/scenes` | GET | 场景清单 |
| `/portal` | GET | 门户（性能前端 / 实时后端 / JEV 联动 / 场景剧本） |

---

## 6. 回滚

```bash
tmux kill-session -t wnidia
mv ~/wnidia ~/wnidia_r9_bad
mv ~/wnidia_r8_bak_<ts> ~/wnidia      # 使用部署时生成的备份
bash ~/wnidia/scripts/deploy_gx10.sh   # MODE=gpu 重启
```

> 部署脚本会在覆盖前自动备份旧版本为 `~/wnidia_rN_bak_<timestamp>`，回滚有据可查。
