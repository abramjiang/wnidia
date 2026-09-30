# 引擎接入指南（Engine Adapter）

本文回答两个问题：**① 怎么接入一个新引擎；② TensorFold 具体怎么接、有哪些前置条件。**

## 一、统一契约

```
必选  POST {base}/chat/completions      {"model":..., "messages":[...]} → OpenAI 响应体
可选  GET  {base}/models                探活与模型名发现（取 data[].id）
可选  GET  {health_url}                 非标准健康端点（TensorFold 用 /health）
```

只要满足必选，引擎就能被调度面统一对待。**不要**为某个引擎在 worker 里写 `if ENGINE == ...`——
`worker.engine_call()` 只认契约，不认厂商。

## 二、接入一个新引擎：四步

### 第 1 步：登记目录

编辑 `controller/engines.py` 的 `ENGINE_CATALOG`：

```python
'myengine': {
    'label': 'MyEngine（一句话定位）',
    'kind': 'openai-compat',          # 或 'mock'
    'default_base': 'http://127.0.0.1:9001/v1',
    'default_model': 'org/model-name',
    'backend': 'nvidia-cuda',         # cpu | nvidia-gb10 | nvidia-cuda | apple-metal | cross-vendor
    'exact': False,                   # ★是否保证与串行解码逐字节一致
    'multi_node': False,              # 是否支持张量并行/多机
    'needs_container': False,         # 是否依赖容器运行时
    'tier_affinity': ['cloud', 'edge'],
    'strengths': ['...'],
    'caveats': ['...'],
    'license': '...',
},
```

同时把它加进 `ORDER`（决定同分时的固定优先级）与（如需要）`select()` 的打分规则。

### 第 2 步：让 worker 可选中

```bash
export WNIDIA_ENGINE=myengine              # 显式指定
# 或 auto 模式下按挡位：
export WNIDIA_GPU_ENGINE=myengine
```

worker 启动时会打印实际绑定的引擎，并写进注册载荷的 `engine` 字段。

### 第 3 步：加一条评测

在 `skills/engine-selector/evals/fixtures/` 里加一份探活快照，在
`skills/engine-selector/evals/evals.json` 里加断言：

```json
{
  "id": "es-myengine-chosen-when-exact-required",
  "category": "correctness", "kind": "positive",
  "run": ["python", "tool.py", "--secret", "L3", "--prefer-exact",
          "--engines-source", "evals/fixtures/probes_myengine.json"],
  "expect": {"exit_code": 0, "json_assert": {"selected": "myengine"}}
}
```

### 第 4 步：真机证据

在 `BENCHMARK.md` §3 记录 before/after：吞吐（tok/s）、p50/p95、GPU 利用率，
**并注明该方法是否逐字节可复现**（记录 `draft: false` 对比结果或 `token_sha`）。

## 三、TensorFold 接入（完整）

### 3.1 它是什么

跨厂商后端的推理引擎：同一份 MLX 4-bit 权重，既能走 Apple Silicon 的 Metal/MLX 内核，
也能走 NVIDIA 的 CUDA/Triton 内核。核心卖点是**逐字节一致**——草稿（speculative draft）
只在"等于逐 token 解码会采样的那个 token"时才被接受，因此**草稿只改变速度、不改变输出**，
回复里带 `token_sha` / `min_rows`，可用 `"draft": false` 自证。

### 3.2 已登记的能力与坑

| 项 | 值 |
|---|---|
| 默认地址 | `http://127.0.0.1:8080/v1`（`--host` 默认就是回环） |
| 默认模型 | `Vontra/Qwen3.8-27B-MLX-4bit`（配 `z-lab/Qwen3.8-27B-DFlash2` 草稿） |
| exact | ✅ 逐字节一致 |
| 多机 | ✅ 双 Spark `--tp 2 --rank R --master HOST` |
| 需要容器 | ✅ CUDA 路径官方只给 NGC 容器（`nvcr.io/nvidia/pytorch:26.07-py3`） |
| 模型覆盖 | 仅 Qwen3.8-27B / Qwen3.8 Flash Next / GLM-5.3-Flash；**Nemotron 无 CUDA 引擎** |
| 权重格式 | 只读 MLX 4-bit；**拒绝 NVFP4 / GPTQ / AWQ** |
| 鉴权 | ❌ HTTP 层无鉴权 → **必须只绑回环** |
| 许可 | 引擎 MIT；权重各自许可；GLM 的 DFlash2 草稿模型为 CC BY-NC-ND（禁商用） |

### 3.3 前置检查（**先做这一步，10 分钟**）

```bash
docker info 2>/dev/null | grep -i -E "nvidia|runtime"
ls /usr/bin/nvidia-container-cli /usr/bin/nvidia-container-runtime 2>/dev/null
```

- **为空** ⇒ 当前节点无法走官方 CUDA 容器路径。手册 8.1-5 允许用 conda/容器隔离 CUDA 版本，
  但 `--gpus all` 依赖 nvidia-container-toolkit；补齐它属于系统级变更，
  **必须先向组委会确认**。在确认前请使用 `ENGINE=ollama`。
- **有输出** ⇒ 可以按 3.4 部署。

`scripts/deploy_gx10.sh` 在 `ENGINE=tensorfold` 时已内置这个检查：
检测不到 nvidia runtime 会直接 `exit 2`，避免"起了个半死不活的引擎"。

### 3.4 部署

```bash
# ① 起 NGC 容器（--network host 便于复用宿主端口；HF 缓存挂载保证下载不丢）
docker run -it --gpus all --ipc=host --network host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/pytorch:26.07-py3

# ② 容器内安装并拉权重（16.1 GB + 3.8 GB —— 手册 8.2-7：>1GB 禁止 scp，必须在节点内下载）
pip install git+https://github.com/ashhart/TensorFold.git
tensorfold pull Vontra/Qwen3.8-27B-MLX-4bit z-lab/Qwen3.8-27B-DFlash2

# ③ 只绑回环（其 HTTP 层无鉴权），端口 8080
tensorfold serve Vontra/Qwen3.8-27B-MLX-4bit --host 127.0.0.1 --port 8080

# ④ 让 WNIDIA 用它
MODE=gpu ENGINE=tensorfold NODE_NUM=<NN> WNIDIA_TOKEN=<强随机> \
  bash scripts/deploy_gx10.sh
```

### 3.5 验证一致性（这是最有说服力的一段演示）

```bash
# 同一请求、同一 seed：开草稿 vs 关草稿，必须逐字节相同
for D in true false; do
  curl -s http://127.0.0.1:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"Qwen3.8-27B-MLX-4bit\",\"draft\":$D,
         \"seed\":1234,\"max_tokens\":64,
         \"messages\":[{\"role\":\"user\",\"content\":\"用一句话解释算力调度\"}]}" \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['choices'][0]['message']['content'])"
done
# 两次输出应当完全一致；回复体里的 token_sha / min_rows 可用于留痕
```

> 演示话术：**「我们不是把模型跑得更快，而是把它跑得更快的同时保证结论不变——
> 而且是可签发证明的那种不变。」**

### 3.6 已知限制（答辩要主动说）

1. 模型覆盖面窄（3 个家族），换模型要重新验证；
2. 与 ollama 同时常驻会争 128 GB 统一内存，需二选一或错峰；
3. 项目版本很新（0.3.x），对 `mlx` 版本敏感，务必锁版本；
4. 若走 GLM-5.3-Flash，其 DFlash2 草稿模型**不可商用**。

## 四、节点 `tier` 与引擎的对应建议

| tier | 建议引擎 | 理由 |
|---|---|---|
| cloud（H100/H200，S1/S2 高优） | vllm / tensorfold | 吞吐优先或可复现优先，视密级定 |
| edge（DGX Spark/GX10） | ollama（现状）/ tensorfold（若能跑 CUDA 路径） | ollama 部署最简；TensorFold 换来可复现 + 更快的草稿解码 |
| home（Mac / Jetson / 消费级 RTX） | tensorfold（Metal 路径） | 同一份权重可在 Apple Silicon 上复用，是"真异构"的证据 |
| cpu（Grace / x86） | mock / 小模型引擎 | 只做兜底与占位 |
