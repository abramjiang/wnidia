# JEV 本地离线评审引擎（local-offline）升级说明

在**不依赖任何外部评审模型 / 云 API** 的前提下，把原 `mock`（规则占位）升级为
可复算、可解释的本地离线评审引擎。

## 变更文件
- 新增 `controller/jev_offline.py`：L0 + L1 + 融合，自包含、纯标准库（L1 用 requests）。
- 改 `controller/jevclient.py`：`mock` 模式内部改用离线引擎，保持 off/mock/live
  三态与原输出字段兼容，新增 `engine / tier / signals / evidence` 可解释字段；
  `status()` 新增 `offline` 段。
- 改 `controller/config.py`：登记 L1 / 校准 / 外部词典配置。
- 新增 `tests/jev_offline_test.py`：42 项校准（归一化、信号、标注对、L1 融合/降级）。

## L0 —— 确定性（离线、零依赖）
- 归一化：Unicode NFKC、全半角、大小写、标点、空白；
  百分比 / 内存(GB,MB,TB) / 质量(kg,g) / 长度(km,m,cm,mm) / 时间(h,min,s) 数字-单位换算；
  紧邻量词的中文数字转阿拉伯（三节点→3节点）；内置同义词 + 虚词停用；
  支持外部同义词词典 `WNIDIA_JEV_SYNONYM_FILE`（JSON）。
- 信号：exact、containment、jaccard、levenshtein、rouge_l、tf_cosine、
  tfidf_cosine（需外部 IDF）、**number 数字事实一致性**。
- 数字是硬事实：关键数字-单位集合冲突时直接压到等价概率 ≤0.12。

## L1 —— 本地 embedding（权重在本机，非外部服务）
- 走本机 Ollama `/api/embeddings`，默认模型 `nomic-embed-text`，向量余弦并入融合。
- 前置（一次性）：安装 Ollama 后 `ollama pull nomic-embed-text`。
- Ollama 不可用 / 超时 / 未安装：自动熔断回退 L0，主流程不报错。
- L1 对词序/方向敏感，可纠正 L0 的"数字换位（提升/下降）"漏判。

## 概率校准
等价概率是相似度的非线性函数：`p = sigmoid(k*(相似度 - mid))`，
默认 mid=0.45、k=8（可用 `WNIDIA_JEV_CAL_MID / WNIDIA_JEV_CAL_K` 调整）。

## 配置（环境变量）
| 变量 | 默认 | 说明 |
|---|---|---|
| WNIDIA_JEV_EMBED | 1 | L1 总开关；0 则只用 L0 |
| WNIDIA_JEV_EMBED_BASE | http://127.0.0.1:11434 | Ollama 地址 |
| WNIDIA_JEV_EMBED_MODEL | nomic-embed-text | 本地嵌入模型 |
| WNIDIA_JEV_EMBED_TIMEOUT | 3 | L1 超时（秒） |
| WNIDIA_JEV_CAL_MID / _K | 0.45 / 8 | logistic 校准参数 |
| WNIDIA_JEV_SYNONYM_FILE | （空） | 外部同义词 JSON |

## 验证结果
- 原 9 套件 **258/258 全部通过，零回归**（沙盒75 / 端到端16 / 对抗40 /
  隐私12 / JEV29 / Skill55 / 策略10 / 集成10 / 压力11）。
- 新增离线校准 **42/42**；标注对准确率：同义 POS 6/6（均≥0.8）、硬负例 NEG 4/4（均<0.5）。
- 自动化合计 **300**。

## 已知边界（答辩口径，诚实）
- "数字换位但数字集合相同"（40%↔72% 且方向相反）L0 集合方法会漏判，需 L1 辨方向。
- 本引擎不声称达到大模型精度；CJK 场景仍遵循"不自动惩罚、低置信转多数决/人工"。
