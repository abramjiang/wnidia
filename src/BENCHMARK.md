# WNIDIA 评测与压测报告（BENCHMARK）

本报告遵循赛事评测口径：**同条件 before/after**、Skill 评测产出 Skill Lift / pass@k、真机以三类证据交叉验证。

- **本地 mock 结果**：用于验证调度/韧性/计量的**程序逻辑正确性**，数字受闭环周期与模拟后端主导，**不代表 GPU 性能**；
- **真机实测结果**：在 DGX Spark 上以 DCGM/Nsight、GenAI-Perf、LM-Eval 采集，下表中以 `【待真机填写】` 标注。

## 1. 评测方法

`bench/load.py` 对 OpenAI 兼容端点发压，固定 `prompt / 请求数 / 并发`，统计：

- 完成数 / 失败数；
- 吞吐（requests/s、tokens/s）；
- 延迟（p50 / p90 / p95 / mean）；
- 控制面平均利用率（真机应改用 DCGM 采样）。

before = 直连**单个** vLLM 实例；after = 经 **WNIDIA 网关**（多实例 + 调度）。除入口外其余条件完全一致。

## 2. 本地 mock 验证（无 GPU，仅证明逻辑）

命令：

```bash
python bench/load.py --target http://127.0.0.1:9000 --token changeme \
  --requests 40 --concurrency 8
```

结果（**mock，请勿用于性能结论**）：

| 指标 | 数值 |
|---|---|
| 完成 / 失败 | 40 / 0 |
| 吞吐 | 1.33 req/s，43.78 tokens/s |
| 延迟 p50 / p95 | 4.085s / 6.512s |
| 平均利用率 | 不适用（mock 突发短于采样间隔） |

> 说明：mock 延迟主要来自 2s 的调度闭环周期与模拟执行，吞吐被人为压低；它验证的是“请求被正确准入、派发、完成、计数”，**不是**真实推理性能。真机延迟与吞吐以 §3 为准。

## 3. 真机 before/after（DGX Spark）

命令：

```bash
python bench/load.py --compare \
  --baseline-url http://127.0.0.1:8200 \
  --wnidia-url  http://127.0.0.1:9000 --token "$WNIDIA_TOKEN" \
  --requests 60 --concurrency 8
```

| 指标 | before（单实例） | after（WNIDIA） | 变化 |
|---|---|---|---|
| 完成 / 失败 | 【待真机填写】 | 【待真机填写】 | — |
| 吞吐 req/s | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 |
| tokens/s | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 |
| 延迟 p50 | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 |
| 延迟 p95 | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 |
| **GPU 利用率** | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 |

> 规模化目标（利用率 85–90% 等）为**产品目标**，仅在 BP 中标 `*`，不与单机实测混用。

## 4. 三类证据（真机采集）

| 证据 | 工具 | 用途 |
|---|---|---|
| 利用率/隔离 | DCGM、Nsight Systems | 多实例/MPS 下的真实利用率与干扰 |
| 推理性能 | GenAI-Perf | TTFT、TPOT、吞吐 |
| 输出质量 | LM-Eval | 切分/共享后质量不回归 |

## 5. Agent Skills 评测

每个技能目录含 `evals/evals.json`（**正例 + 负例**），覆盖五维：security / correctness / discoverability / effectiveness / efficiency。

- **Skill Lift**：同一任务在“带技能 vs 不带技能”下的成功率/质量差值；
- **pass@k**：k 次调用中至少一次通过的比例；
- 评测流程对齐 SkillEvaluator：Tier1 结构校验（含 SkillSpector）→ Tier2 去重 → Tier3 沙箱实跑。

| 技能 | 正例 | 负例 | 关键断言 |
|---|---|---|---|
| resource-doctor | 异常/健康两夹具 | 文件缺失、garbage | 正确给出 node_lost/low_free_mem/untrusted/healthy |
| gpu-slicer | 混合负载选 MPS、超分 defer、MIG 回退 | 坏 workload | 实例 util 之和不超显存上限 |
| smart-dispatcher | chat→decode、高优抢占、机密仅可信 | 全离线、坏任务 | 选址与抢占标志正确、可解释 |
| idle-onboarding | dry-run、成功纳管 | 探活失败、缺参数、坏 tier | 探活失败不注册、初始信誉 1.0 |

## 6. Jev 决策增强层（模型已替换为开源实现）

决策层以“类型化问题 → 校准概率”提供三类增强；**确定性安全门禁始终权威**，模型只产出建议、可日志、可回滚：

| 增强 | 机制 | 安全约束 |
|---|---|---|
| 多数决语义判定 | 语义等价 + 证据支持 + 标签（honest/off_topic/tampered/hallucinated） | 消除同义改写误判、识别篡改 |
| 选择性 / 自适应校验 | 普通任务廉价预检，仅低置信才补三节点 | 显式 verify 仍恒跑多数决 |
| 入口护栏 | 注入 / 越权 / 隐私预检 | 仅高置信命中才阻断，隐私类只复核 |

### 6.1 选型（开源替代评估）

默认模型由 TypeSafe 商业 `typesafe/jev-1.13` 替换为 **`heman10x/openJev-verdict-2.0`**（151M ModernBERT，非自回归，RLCD 校准；LocalLLaMA/typed-decisions：acc 77.10% / Brier 0.0636 / ECE 0.0144，优于 TypeSafe Jev 与 Laya）。备选对比：

| 候选 | 定位 | 结论 |
|---|---|---|
| **openJev-verdict-2.0** | 151M ModernBERT，非自回归，RLCD 校准 | **选用**：最高分、最小（CPU/1GB 可跑）、可自托管 |
| Laya | ModernBERT，PyPI `laya`，vLLM 原生 `/v1/systemone` | 备选：http 后端直接兼容（`JEV_MODEL`/`JEV_BASE` 切 vLLM 即可） |
| NanoJev | 0.6B，带端到端训练流水线 | 备选：需自训时用，重量更重 |
| Nimble | “fast encoder” 复刻 | 备选：精度/生态弱于 openJev-verdict-2.0 |

### 6.2 模式与后端

- 模式 `off / mock / live / auto`（env `WNIDIA_JEV_MODE`），默认 **mock**：本地确定性占位，输出形状与真实模型一致，**不代表真机精度**；
- 后端（env `WNIDIA_JEV_BACKEND`）：`http`（Jev 兼容 `/v1/systemone`，TypeSafe/Laya/`scripts/serve_jev.py` 自建均可）｜`local`（进程内 transformers 加载 openJev 权重）；缺依赖/权重/端点不可达/超时/无密钥自动降级为 None，系统行为不劣化；
- 真实模型在中文（CJK）场景不信任自动惩罚 → 回退精确匹配；
- 成本/延迟的规模化收益与 openJev 精度须真机 before/after 标定（DCGM / GenAI-Perf / LM-Eval），标 `*`，不与厂商宣称或 mock 混用。

自建服务：`pip install -r requirements-jev.txt && python scripts/serve_jev.py`，再设 `WNIDIA_JEV_MODE=live WNIDIA_JEV_BACKEND=http WNIDIA_JEV_BASE=http://127.0.0.1:8201`。

## 7. 端到端、对抗与 Jev 自检

```bash
# 统一用 WNIDIA_PY / EVAL_PYTHON 指定解释器，避免评测环境与部署环境不一致
WNIDIA_PY=$(which python3) python tests/e2e_test.py          # 常规全链路
WNIDIA_PY=$(which python3) python tests/adversarial_test.py  # 对抗/并发/边界
python tests/jev_test.py                                     # Jev 决策增强层（纯单元）
EVAL_PYTHON=$(which python3) python skills/run_evals.py      # 5 技能评测
```

> ⚠️ 本轮修正了一个**评测可信度**问题：`run_evals.py` 此前只实现了
> `exit_code / json_assert / json_keys / json_nonempty / json_contains /
> json_candidate_nodes` 六个断言键，而各 skill 的 `evals.json` 里其实还写了
> `json_instance_count`、`json_max`、`json_contains_codes`、`no_traceback_leak`
> 四个键——它们被**静默忽略**。如今已全部实现，于是立刻暴露了 2 个真实缺陷
> （详见 v3 记录 `docs/BUG-HUNT-3ROUNDS-v3.md`）。**"全绿"必须建立在断言真的跑起来之上。**

- `e2e_test.py`：健康、鉴权拒绝、3 节点在线、节点引擎画像、引擎目录可枚举、
  引擎选路可解释、合规自检可查、网关问答、多数决识别作弊（信誉 1.0→0.8）、
  高优抢占（带真实 progress）、抢占后自动恢复、无任务卡在 binding、
  掉线事件、隔离期内保持离线、Agent 暴露 9 个 Skill、Agent 路由到 engine-selector。
- `adversarial_test.py`：原 A–I（抢占/恢复竞态连续 8 轮无挂死、2 节点多数决守卫、
  空/超长/异常输入不崩溃、重复抢占优雅处理、鉴权拒绝、隔离期粘住 + 隔离期后恢复、
  重复注册覆盖画像、合规视图端口与脱敏、引擎降级链与一致性说明、引擎探活非法参数不 500）
  **+ v4 新增 J–R 共 22 条断言**：
  - J 批量入队非法入参（空列表 / 非法 sla + 非数值 / 缺 `tasks`）
  - K 断网批次回放**幂等** + 缺 `batch_id` 被 422 + 控制面计入补偿
  - L 门槛五条齐全、样本不足**不判 GO**、每条都报样本量
  - M 结算重复执行**不产生重复记录**、分成比例显式标注为规划假设
  - N QPU 超上限量子位 / `cudaq` 未安装（拒绝而非静默回退）/ 未知门
  - O 未注册机型灰度不崩 / 非法灰度比例被拒 / 未注册机器人回传不 500
  - P 分时：时片大于窗口被拒 / 零窗口被拒
  - Q 非法 `secret` / 非法 `cc_required` 不打断闭环
  - R 证据包签名可校验、**篡改可检出**、默认不含 prompt 明文
- `jev_test.py`：off 回退、护栏阻断/复核/放行、预检、语义等价（同义改写不罚、篡改识别）、
  live 中文门控、live 失败降级。结果：**29/29 通过**。
- 技能评测（`run_evals.py`，9 个 Skill）：**55/55 通过**。
- prompt 隐私单测（`tests/prompt_privacy_test.py`，v5 新增）：**12/12 通过**
  —— 覆盖"两个开关是「与」关系"、派发闸门、落库闸门与历史明文清理。

## 8.5 全能力沙盒验证（★v4 新增）

比逐条单测更进一步：**一键起整栈 → 按 BP 能力面逐项断言 → 结构化落盘**。

```bash
WNIDIA_PY=$(which python3) python scripts/sandbox_v4.py            # A–M 全部
WNIDIA_PY=$(which python3) python scripts/sandbox_v4.py --only BC  # 只跑指定场景
WNIDIA_PY=$(which python3) python scripts/sandbox_v4.py --keep     # 保留栈
```

14 个场景 / 75 条断言：

| 场景 | 覆盖 | 关键断言 |
|---|---|---|
| A 口径与能力面 | `/admin/capabilities` | 对外只有三层；`secret` 与 `cc` 分离；`decision_only` 的措辞与标签一致 |
| B 三维路由（时延） | P5 | 节点已上报时延；**存在超预算节点（硬约束确实在筛）**；决策时命中的节点满足预算；**不可满足预算被明确拒绝**（`latency_budget_unmet`） |
| C SLA 优先级排队 | P13 L2 | 同批入队的两任务，**后创建的高优先出队** |
| D 真实计量与分账 | P10 | 采到引擎 usage；三口径并存；租户/项目/部门分账齐全；计量质量含 `real_ratio` |
| E 机密计算与证明 | P9 | 证明签发与校验；L3 任务落在具备 CC 能力的节点 |
| F 门槛度量 | P21 | 五条门槛全部评估、都报样本量、样本不足不判 GO、有收缩线动作 |
| G 边缘断网自治 | P6/P13/P14 | 本地入队 → 断网态 → 批次回放 → 控制面记录 → **重复提交幂等** |
| H 具身机队 | P18 | 注册 / 灰度三阶段 / 分配确定性 / 派发 / **回传脱敏** |
| I QPU 抽象 | P18 | 后端枚举 / PQC 标 `ready=false` / Bell 态两态分布 / 拒绝 `cudaq` / 超上限被拒 |
| J 审计证据包 | P9 CC-L3 | 导出 → 校验通过 → **篡改后校验失败** → 审计链完整 |
| K 机主结算 | P7 | 结算落库；分成比例为规划假设并显式标注；记录可查；**同周期重复结算幂等（先清后写，不重复计账）** |
| L 分时复用策略 | P13 L2 | 排期生成、份额归一、`policy_only`、非法窗口被拒 |
| M SDK | P13 | health / capabilities / metering / gates / 租户用量全接口可用 |
| N prompt 隐私 ★v5 | P13 决策③ | 改造后任务仍能完成；**直查 SQLite 无明文**；`/admin/state`、看板 `/api/state`、租户用量、证据包全部脱敏；脱敏后仍留摘要+长度；请求 `include_prompts` 而开关未开时如实拒绝；`miss_active=0` |

结果：**75/75 通过**，明细落盘 `data/sandbox_v4_result.json`。

> 沙盒设计上刻意的两处诚实性约束：
> 1. **时延断言核对的是"决策时记录的时延"**，不是任务结束后重读的当前值
>    （时延每个心跳都在变，"当时合规、现在超了"是常态，拿后者当断言本身就是错的）；
> 2. **"不可满足的时延预算"用 0.001ms 构造**（低于上报精度 0.01ms，构造上不可能满足），
>    而不是"当前最小实测值的一半"——后者会随心跳波动产生竞态假失败。

---

## 8. 引擎对比（★新增，回答"换引擎值不值"）

### 8.1 能力矩阵

| 维度 | mock | ollama | vllm | tensorfold |
|---|---|---|---|---|
| 后端 | CPU（进程内） | nvidia-gb10 | nvidia-cuda | **cross-vendor**（Metal/MLX ↔ CUDA/Triton） |
| 逐字节可复现 | ✅（确定性函数） | ❌ | ❌ | ✅（草稿只改速度不改字节） |
| 多机张量并行 | ❌ | ❌ | ✅ | ✅（双 Spark `--tp 2`） |
| 依赖容器 | ❌ | ❌ | ❌ | ✅（NGC，需 nvidia runtime） |
| 模型覆盖 | 任意（占位） | 最广（GGUF） | 广 | 窄（3 个家族） |
| 权重格式 | — | GGUF | NVFP4/FP16… | 仅 MLX-4bit（拒绝 NVFP4/GPTQ/AWQ） |
| 鉴权 | — | 无（绑回环） | 无（绑回环） | 无（**必须绑回环**） |
| 定位 | 流程与韧性自证 | 部署最简、兜底 | 高吞吐批处理 | 可复现 + 加速 |

### 8.2 真机 before/after（DGX Spark · Qwen3.8-27B 档位）

| 指标 | 直连单引擎（before） | 经 WNIDIA（after） | 说明 |
|---|---|---|---|
| 吞吐（req/s） | 【待真机填写】 | 【待真机填写】 | 同口径：`bench/load.py --compare` |
| 吞吐（tokens/s） | 【待真机填写】 | 【待真机填写】 | |
| p50 延迟（ms） | 【待真机填写】 | 【待真机填写】 | |
| p95 延迟（ms） | 【待真机填写】 | 【待真机填写】 | |
| GPU 利用率 | 【待真机填写】 | 【待真机填写】 | DCGM / `nvidia-smi` 采样 |
| 结果一致性 | 【待真机填写】 | 【待真机填写】 | 同 seed 重复 N 次，统计输出是否逐字节相同 |

### 8.3 引擎横向对比（同机同模型，逐项实测）

| 引擎 | tok/s | p50（ms） | GPU 利用率 | 同 seed 输出是否逐字节一致 | 数据来源 |
|---|---|---|---|---|---|
| mock | 不适用 | 不适用 | 不适用 | 是（确定性函数） | `BENCHMARK.md §2` |
| ollama（27B GGUF） | 【待真机填写】 | 【待真机填写】（历史实测 p50 ≈ 32s） | 【待真机填写】 | 否 | 真机 |
| vllm | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 | 否 | 真机 |
| tensorfold | 【待真机填写】 | 【待真机填写】 | 【待真机填写】 | **是**（`draft:false` 对比 + `token_sha`） | 真机 |

> 采集要求：
> 1. 单流、固定 64-token 回复、固定 seed，chat 与 code 两类 prompt 各跑一轮；
> 2. 每项重复 ≥5 次取中位数，同时记录 p50/p95；
> 3. 一致性一栏必须给出方法（`draft:false` 对比、`token_sha` 或重复 N 次哈希比对），
>    **不允许**用"看起来一样"充当"逐字节一致"；
> 4. 未采集的格子保留【待真机填写】，不得用估算值或厂商宣称值填充。

### 8.4 选路行为的可复现性验证（离线，无需 GPU）

```bash
EVAL_PYTHON=$(which python3) python skills/run_evals.py skills/engine-selector
# 9 条断言：L1 chat → tensorfold；L3 → 强制 exact 且 consistency_satisfied=true；
# heavy → vllm（吞吐）；TensorFold 不可用时降级到 ollama 并显式标注
# "不提供逐字节一致性保证"；全部不可用 → 回落 mock
```
