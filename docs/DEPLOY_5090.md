# WNIDIA 部署：NVIDIA RTX 5090 变体

> ⚠️ **未实机验证声明**
> 本文档与 `scripts/deploy_5090.sh` 依据 RTX 5090 的公开规格编写，
> **尚未在真实 5090 硬件上运行验证**。首次使用请先跑 `MODE=mock` 验证流程，
> 再切 gpu 模式。遇到偏差请提 issue 并附上实际环境信息。
>
> 参赛提交版本仍基于 **GB10**（见 `scripts/deploy_gx10.sh`）；本文档是**扩展路径**，
> 不属于参赛提交内容。

---

## 1. 为什么先做 5090

异构编排的价值来自**设备之间的差异**。5090 与 GB10 恰好构成最强互补：

| 维度 | GB10 | RTX 5090 | 差异倍数 |
|---|---|---|---|
| 容量 | **128 GB 统一内存** | 32 GB | GB10 约 4× |
| 带宽 | ≈273 GB/s | **≈1792 GB/s** | 5090 约 6.5× |
| 算力 | 中 | **高** | — |
| 精度 | FP4 / FP8 | FP4 / FP8 | 同代 Blackwell，一致 |
| 功耗 | ≈170–240W（待核） | 575W | 5090 显著更高 |

**结论**：5090 补上了 GB10 最缺的**带宽与算力**，GB10 补上 5090 最缺的**容量**。
二者配对，才能真正演示「容量 × 带宽 × 算力」的三角权衡——**单独任何一张卡都做不到**。

（若 5090 受供电/预算限制，退而求其次选 4090：带宽 1008 GB/s，仍是 GB10 的 3.7 倍，
但容量仅 24GB 且无 FP4，互补性弱于 5090。）

---

## 2. 与 GB10 版本的差异

| 项 | GB10 版 | 5090 版 |
|---|---|---|
| 网络 | 固定公网 IP + SSH 端口映射 | 不绑定固定地址，默认本地 `127.0.0.1`，可用 `PUBLIC_IP` 覆盖 |
| GPU 画像 | `GPU_NAME=GB10`，40GB 预算 | `GPU_NAME=RTX-5090`，默认 28GB 预算（32GB 预留 4GB） |
| 引擎 | Ollama | **默认 vLLM**（更好批处理/吞吐，吃满高带宽）；Ollama 可作回退 |
| 量化 | 由 Ollama 决定 | 显式 `QUANT`（默认 fp8，可选 fp4 / int8 / fp16） |
| worker | cloud / edge / cpu 三档 | gpu-5090（decode 主力）+ cpu |

---

## 3. 前置条件

- NVIDIA 驱动 + CUDA（Blackwell 需较新版本，**请按实际环境核对**）
- `python3`、`tmux`
- 若用 vLLM：`pip install vllm`（体积较大）
- **供电与散热**：5090 满载约 575W，务必确认电源余量与机箱风道

---

## 4. 快速开始

```bash
# 1) 先用 mock 模式跑通流程（不依赖 GPU）
bash scripts/deploy_5090.sh

# 2) 起 vLLM（gpu 模式）
pip install vllm
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8100 \
     --gpu-memory-utilization 0.9 --quantization fp8 --max-model-len 8192

# 3) 再以 gpu 模式启动 WNIDIA
MODE=gpu bash scripts/deploy_5090.sh

# 可选：用 Ollama 代替 vLLM（更简单，吞吐略低）
MODE=gpu ENGINE=ollama bash scripts/deploy_5090.sh
```

可调环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `VRAM_BUDGET_GB` | 28 | 显存预算（32GB 卡建议留 4GB 给 KV/碎片） |
| `QUANT` | fp8 | 量化策略（fp4 / fp8 / int8 / fp16） |
| `PUBLIC_IP` | 127.0.0.1 | 对外访问地址 |
| `VLLM_BASE` / `VLLM_MODEL` | 8100 / Qwen2.5-7B | 引擎端点与模型 |

---

## 5. 停止与排查

```bash
tmux attach -t wnidia      # 查看窗口：api / dash / agent / gpu / cpu
tmux kill-session -t wnidia # 停止
curl -H 'Authorization: Bearer <Token>' http://127.0.0.1:9000/admin/compliance
```

---

## 6. 实机验证清单（待完成）

- [ ] 在真实 5090 上跑通 `MODE=mock` 与 `MODE=gpu` 两种模式
- [ ] 记录 vLLM 与 Ollama 的 P50/P95 时延与吞吐，与 GB10 对比
- [ ] 测出 32GB 显存下可跑的最大模型规模（含 KV cache）
- [ ] 验证 FP4 / FP8 对业务指标的影响
- [ ] 记录满载功耗与温度，确认散热余量
- [ ] 把实测数据回填本文档，并移除"未实机验证"声明

---

## 7. 与异构混合架构的关系

5090 的接入方式遵循**设备画像（DeviceProfile）**思路：新增一张卡只需注册其
容量 / 带宽 / 算力 / 精度四维画像，无需改动调度逻辑。
详见 `docs/ARCHITECTURE.md` 与 `docs/SELFCHECK.md`。

下一步建议：实现 **GB10 + 5090 双节点混合调度**（按任务特征路由：
大模型/长上下文 → GB10；高并发 decode → 5090）。
