# -*- coding: utf-8 -*-
"""全局配置：全部可通过环境变量覆盖，便于在 DGX Spark 上部署。"""
import os
from pathlib import Path


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on', 'y')


# ---- 网络 / 端口（对齐 Spark 手册：8888 看板、9000 API）----
# 部署形态：spark=标准节点；gx10=实际 gx10 节点（公网 7026/8888/8026，无9000）
DEPLOY_TARGET = os.getenv('WNIDIA_DEPLOY_TARGET', 'spark').strip().lower()
API_PORT   = _int('WNIDIA_API_PORT', 9000)
DASH_PORT  = _int('WNIDIA_DASH_PORT', 8888)
HOST       = os.getenv('WNIDIA_HOST', '0.0.0.0')
# 集群编号 NN（51–100）：用于按手册 1.2 换算公网端口 6NN/8NN/9NN
NODE_NUM   = os.getenv('NODE_NUM', os.getenv('SPARK_NN', ''))
PUBLIC_IP  = os.getenv('SPARK_PUBLIC_IP', '<PUBLIC_IP_2>')

# ---- 鉴权（部署时务必通过环境变量修改默认值）----
API_TOKEN  = os.getenv('WNIDIA_TOKEN', 'changeme')
DASH_USER  = os.getenv('WNIDIA_DASH_USER', 'reviewer')
DASH_PASS  = os.getenv('WNIDIA_DASH_PASS', 'wnidia2026')
# 1=启动时执行合规自检（弱口令/绑定地址）。仅本地 mock 演示可设 0 绕过。
COMPLIANCE_STRICT = _bool('WNIDIA_COMPLIANCE_STRICT', True)

# ---- 存储 ----
ROOT       = Path(os.getenv('WNIDIA_ROOT', Path(__file__).resolve().parents[1]))
DATA_DIR   = ROOT / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH    = Path(os.getenv('WNIDIA_DB', str(DATA_DIR / 'wnidia.db')))

# ============ 推理数据（prompt）留存策略 ============
# 背景：BP P13 决策③ 写「控制面/数据面分离、推理数据留本地」。
# 默认 False = **prompt 不落库明文**：tasks.prompt 只写占位标记，另存摘要与长度；
# 调度所需的那份 prompt 只活在控制面进程内存里（见 controller/prompt_guard.py），
# 任务终态即释放。开启（=1）才写明文，仅用于本地复盘，且会打印合规提示。
STORE_PROMPT         = _bool('WNIDIA_STORE_PROMPT', False)
# 对外输出（/admin/state、租户用量、SDK）默认**脱敏**；=1 时才允许返回全文。
# 与 STORE_PROMPT 是「与」关系：只开这一个也拿不到明文，避免单开关即泄露。
REVEAL_PROMPT        = _bool('WNIDIA_REVEAL_PROMPT', False)
# 脱敏时给出的预览长度（字符）
PROMPT_PREVIEW_CHARS = _int('WNIDIA_PROMPT_PREVIEW', 48)
# 启动时把历史库里遗留的 prompt 明文一并抹掉（老库从 v3/v4 升级上来时用）。
# 关掉它只影响"历史遗留"，不影响新任务（新任务一律按 STORE_PROMPT 处理）。
SCRUB_LEGACY_PROMPTS = _bool('WNIDIA_SCRUB_LEGACY', True)
# 内存 spool 容量上限（条）。超出按最早写入淘汰，并计入 dropped 计数。
PROMPT_SPOOL_MAX     = _int('WNIDIA_PROMPT_SPOOL_MAX', 4000)

# ---- 调度参数 ----
HEARTBEAT_TIMEOUT_S = _int('WNIDIA_HB_TIMEOUT', 15)   # 心跳超时判离线
LOOP_INTERVAL_S     = _int('WNIDIA_LOOP_INTERVAL', 2)
WORKER_CALL_TIMEOUT = _int('WNIDIA_CALL_TIMEOUT', 60)

# ---- 租户默认配额（演示用，单位：模拟算力积分）----
DEFAULT_QUOTA = _int('WNIDIA_DEFAULT_QUOTA', 1000)

# ---- 重排上限：超过即判失败，避免任务无限重排 ----
MAX_ATTEMPTS = _int('WNIDIA_MAX_ATTEMPTS', 5)

# ---- 绑定超时：定时回收卡在 binding 的任务（原实现无归宿）----
BINDING_TIMEOUT_S = _int('WNIDIA_BINDING_TIMEOUT', 60)

# ============ Agent 决策策略（v5.1：LLM 提议 → 内核裁决） ============
# off     ：现状，确定性内核独占决策（默认，零行为变化）
# shadow  ：Agent 与内核双轨各算各的，只留痕不影响派发——用于攒一致率
# enforce ：提议通过裁决即采纳，否则回落 scheduler.match（零风险兜底）
AGENT_POLICY   = os.getenv('WNIDIA_AGENT_POLICY', 'off').strip().lower()
# Agent 服务地址与提议调用预算：控制面主循环不能被 LLM 长调用拖住，超时即回落
AGENT_BASE     = os.getenv('WNIDIA_AGENT_BASE', 'http://127.0.0.1:7000')
AGENT_PROPOSE_TIMEOUT = _float('WNIDIA_AGENT_PROPOSE_TIMEOUT', 6.0)

# ---- 运行模式：auto / gpu / mock ----
MODE = os.getenv('WNIDIA_MODE', 'auto')   # worker 侧据此决定 vLLM 或 mock

# ============ Jev 决策增强层 ============
# 后端：http 走 Jev 兼容的 /v1/systemone（TypeSafe / Laya / 自建 openJev 服务均可）；
#       local 在进程内用 transformers 直接加载开源 openJev 权重（无需外部服务）。
# 选型：默认替换为 openJev-verdict-2.0（151M ModernBERT，非自回归，RLCD 校准，
#       在 LocalLLaMA/typed-decisions 上 acc 77.10% / Brier 0.0636 / ECE 0.0144，
#       优于 TypeSafe Jev 与 Laya），可自托管；Nimble/NanoJev 详见 BENCHMARK §6。
JEV_BACKEND    = os.getenv('WNIDIA_JEV_BACKEND', 'http')   # http | local
# off | mock | live | auto（auto：有 JEV_KEY 或 local 后端走 live，否则 mock）
JEV_MODE       = os.getenv('WNIDIA_JEV_MODE', 'mock')
JEV_BASE       = os.getenv('WNIDIA_JEV_BASE', 'http://127.0.0.1:8201')
JEV_KEY        = os.getenv('WNIDIA_JEV_KEY', '')
JEV_MODEL      = os.getenv('WNIDIA_JEV_MODEL', 'heman10x/openJev-verdict-2.0')
JEV_TIMEOUT    = _int('WNIDIA_JEV_TIMEOUT', 8)
# 语义等价阈值：超过即视为同一答案（消除同义改写误判）
JEV_SEMANTIC_MIN = _float('WNIDIA_JEV_SEMANTIC_MIN', 0.8)
# 选择性校验：预检 P(正确) 低于此值才触发三节点多数决
JEV_VERIFY_TRIGGER = _float('WNIDIA_JEV_TRIGGER', 0.6)
# 置信度高于此值才允许 Jev 建议自动生效（阻断/惩罚）
JEV_CONF_AUTO  = _float('WNIDIA_JEV_CONF_AUTO', 0.85)
# 入口护栏（注入/越权/隐私）总开关
JEV_GUARDRAIL  = _bool('WNIDIA_JEV_GUARDRAIL', True)
# 普通任务自适应校验：预检低置信时自动补做多数决
JEV_ADAPTIVE   = _bool('WNIDIA_JEV_ADAPTIVE', True)

# ---- 本地离线评审引擎 local-offline（controller/jev_offline.py；mock 模式使用）----
# 说明：jev_offline 为保持可独立运行，下列变量在该模块内直接用 os.getenv 读取，
#       此处集中登记默认值，二者保持一致。
# L1 本地 embedding 总开关（走本机 Ollama；不可用/超时自动回退 L0，不报错）
JEV_EMBED_ENABLE  = _bool('WNIDIA_JEV_EMBED', True)
JEV_EMBED_BASE    = os.getenv('WNIDIA_JEV_EMBED_BASE', 'http://127.0.0.1:11434')
JEV_EMBED_MODEL   = os.getenv('WNIDIA_JEV_EMBED_MODEL', 'nomic-embed-text')
JEV_EMBED_TIMEOUT = _float('WNIDIA_JEV_EMBED_TIMEOUT', 3.0)
# 等价概率 logistic 校准：p = sigmoid(k*(相似度 - mid))
JEV_CAL_MID = _float('WNIDIA_JEV_CAL_MID', 0.45)
JEV_CAL_K   = _float('WNIDIA_JEV_CAL_K', 8.0)
# 可选外部同义词词典 JSON（{短语: 规范词, 虚词: ""}），缺省只用内置词典
JEV_SYNONYM_FILE = os.getenv('WNIDIA_JEV_SYNONYM_FILE', '')

# ============ 引擎可插拔层（Engine Adapter） ============
# auto：按 MODE 推导（gpu→WNIDIA_GPU_ENGINE，否则 mock）
# 可选：mock | ollama | vllm | tensorfold | triton | nim
ENGINE           = os.getenv('WNIDIA_ENGINE', 'auto')
GPU_ENGINE       = os.getenv('WNIDIA_GPU_ENGINE', 'ollama')
ENGINE_TIMEOUT   = _int('WNIDIA_ENGINE_TIMEOUT', 3)      # 探活超时（秒）
# 需要逐字节可复现的任务（如举证、审计）自动要求 exact 引擎
EXACT_FOR_SECRET = os.getenv('WNIDIA_EXACT_FOR_SECRET', 'L3,L4')
