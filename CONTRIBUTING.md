# 贡献指南 · CONTRIBUTING

感谢参与 **WNIDIA** 项目！本指南面向赛事协作与开源贡献。

## 🧰 开发环境

- Python 3.9+（推荐 3.13）
- Node 18+
- 推理引擎：Ollama（演示默认）、vLLM / TensorFold（生产可选）

## 🌿 分支模型

- `main`：稳定可演示版本（赛事提交基线）
- `dev`：集成开发分支
- `feature/*`：特性分支，PR 合入 `dev`，经评审后进入 `main`

## 📝 提交规范（Conventional Commits）

| 前缀 | 含义 |
|---|---|
| `feat:` | 新功能 |
| `fix:` | 缺陷修复 |
| `docs:` | 文档 |
| `perf:` | 性能 |
| `test:` | 测试 |

示例：`feat(scheduler): 新增边缘自治回放策略`

## 🔧 本地联调

```bash
MODE=preview bash scripts/deploy_gx10.sh
```

启动后访问看板 `:8888`，触发 `http://localhost:9000/admin/demo/run?scene=edge` 验证端到端链路。

## ✅ 提交前 Checklist

- [ ] 本地 `preview` 可一键启动，6 个窗口无报错
- [ ] JEV 报告 `/admin/jev/report` 可返回真实指标（tokens / 时延 / 收益）
- [ ] `jev_log.jsonl` 正确落盘
- [ ] 文档与代码行为一致
- [ ] 无密钥 / 大体积日志被提交（见 `.gitignore`）

## 📮 提交 PR

1. Fork 并切出 `feature/*` 分支
2. 本地自测通过后发起 PR 至 `dev`
3. 维护者评审通过后合入
