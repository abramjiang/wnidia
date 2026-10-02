# JEV live 开发成果（路线 A / 路线 B）

> **重要说明：本目录内容为「下一版开发成果」，不属于参赛提交版本的源码。**
>
> - 仓库 `src/` 为 **r9 提交快照**（已脱敏），与参赛提交物严格一致，**保持冻结**。
> - 参赛提交实例中 JEV 一致性裁决为 mock 模式（实测 48 次调用全部 mock、0 次 live，裁决服务未启用）。
> - 本目录用于在**开发机 / 自备环境**把 JEV 的 live 裁决真正跑起来，供下一版演进与答辩补充演示使用。
> - **禁止在已截止部署的参赛节点上执行本目录中的任何内容。**

---

## 为什么单独成目录

`src/` 代表"已提交的那一版"。截止后再把 live 代码混进去，会让评审误以为提交版已具备 live 能力。
因此把新开发的内容独立放置在 `jev-live/`，并在此明确标注——既可展示技术演进，又不污染提交快照。

---

## 路线对比

| 路线 | 做法 | 依赖 | 改动量 |
|---|---|---|---|
| **A（local）** | 进程内 transformers 加载开源 openJev 权重做真实裁决 | `transformers` + `torch` + 权重（151M ModernBERT） | **零改码**，仅配置 |
| **B（multi）** | 用 Ollama 多模型独立作答并做一致性多数决 | 本机 Ollama + ≥2 个模型 | 需按 `PATCH.md` 改 3 个文件 |

---

## 目录内容

| 文件 | 说明 |
|---|---|
| `jev_multi.py` | 路线 B 的多模型交叉裁决模块（放到 `controller/` 下即可用） |
| `jev_live_setup.sh` | 路线 A / B 一键启用与验收脚本（放到 `scripts/` 下） |
| `PATCH.md` | 路线 B 所需的精确改动（config.py / jevclient.py 共 6 处） |
| `README.md` | 本文件 |

---

## 快速开始

```bash
# 1) 放入工程
cp jev_multi.py        <工程>/controller/
cp jev_live_setup.sh   <工程>/scripts/
# 2) 按 PATCH.md 应用 6 处改动
# 3) 启用（开发机）
cd <工程>
./scripts/jev_live_setup.sh A     # 路线 A
./scripts/jev_live_setup.sh B     # 路线 B
source scripts/jev_live.env
MODE=preview bash scripts/deploy_gx10.sh
```

---

## 验收标准（live 真正生效）

| 指标 | mock 时 | 生效后应看到 |
|---|---|---|
| `summary.mode` | `mock` | **`live`** |
| `summary.live` | `0` | **> 0** |
| `dist.na` | 全量 | **出现 `ok` / `div`** |
| `consistency_ok` | `null` | `1` / `0` |

---

## ⚠️ 两个必须知道的坑

1. **CJK 门控**：原实现 `trust = (not cjk) and min_conf >= JEV_CONF_AUTO`——只要内容含中日韩文字，`trust` 恒为 False（设计上避免中文误判惩罚）。
   中文场景必须设 `WNIDIA_JEV_CJK_TRUST=1`，否则开了 live 也仍是全 `na`。改动见 `PATCH.md` 第 6 处。

2. **置信度阈值**：路线 B 的置信度 = 相似度，默认 `JEV_CONF_AUTO=0.85` 过高，建议下调至 `0.60`。

---

## 验证状态

- `jev_multi.py`、`config.py`、`jevclient.py` 均通过 `py_compile`；`jev_live_setup.sh` 通过 `bash -n`。
- 多模型裁决逻辑经桩测（7 项断言全部通过）：一致候选判 `honest`、分歧候选判 `hallucinated`、护栏类返回保守值。
- 开发机缺少 Ollama / transformers 时，脚本会给出明确安装指引并以退出码 2 结束（已实测）。
