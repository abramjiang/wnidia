# -*- coding: utf-8 -*-
"""引擎可插拔层（Engine Adapter Layer）。

把「推理引擎」从写死的实现，变成一份**可注册、可探活、可排序、可降级**的目录。
这是本次升级的核心：调度面不再假设后端一定是 vLLM 或 Ollama。

统一契约（所有 openai-compat 引擎必须满足）：
    POST {base}/chat/completions   {"model":..., "messages":[...]} → OpenAI 响应体
    可选 GET  {base}/models        用于探活与模型名发现
    可选 GET  {health_url}         非 OpenAI 标准的健康端点（如 TensorFold /health）

只读回环：引擎一律绑 127.0.0.1（手册第五章：非映射端口用 SSH 隧道），
对外由控制面 :9000 统一鉴权代理。该约束由 compliance.assert_probe_allowed 强制。
"""
import os
import time

from . import compliance

# ---------------------------------------------------------------- 目录
CATALOG = {
    'mock': {
        'label': 'Mock 模拟引擎',
        'kind': 'mock',
        'default_base': '',
        'default_model': 'mock-compute',
        'backend': 'cpu',
        'exact': True,              # 本地确定性函数，天然可复现
        'multi_node': False,
        'needs_container': False,
        'tier_affinity': ['cpu', 'home'],
        'strengths': ['离线可用', '零依赖', '逻辑自证'],
        'caveats': ['不产生真实 token', '仅供流程与韧性验证'],
        'license': 'n/a',
    },
    'ollama': {
        'label': 'Ollama（GGUF 单引擎）',
        'kind': 'openai-compat',
        'default_base': 'http://127.0.0.1:11434/v1',
        'default_model': 'modelscope.cn/unsloth/Qwen3.8-27B-GGUF:latest',
        'backend': 'nvidia-gb10',
        'exact': False,
        'multi_node': False,
        'needs_container': False,
        'tier_affinity': ['edge', 'home'],
        'strengths': ['部署最简', '系统服务常驻', '权重格式兼容性最好'],
        'caveats': ['批处理与算子顺序不定，同一 prompt 两次结果可能不同',
                    '单引擎串行，p50 延迟高'],
        'license': '模型各自许可（GGUF 权重）',
    },
    'vllm': {
        'label': 'vLLM（NVFP4 / 张量并行）',
        'kind': 'openai-compat',
        'default_base': 'http://127.0.0.1:8101/v1',
        'default_model': 'nemotron-nvfp4',
        'backend': 'nvidia-cuda',
        'exact': False,
        'multi_node': True,
        'needs_container': False,
        'tier_affinity': ['cloud', 'edge'],
        'strengths': ['吞吐最高', '支持张量并行', 'NVFP4 权重体积小'],
        'caveats': ['独立进程占显存，与 Ollama 不能常驻共存',
                    '默认不保证逐字节可复现'],
        'license': 'Apache-2.0（引擎）；模型各自许可',
    },
    'tensorfold': {
        'label': 'TensorFold（跨厂商精确解码）',
        'kind': 'openai-compat',
        'default_base': 'http://127.0.0.1:8080/v1',
        'default_model': 'Vontra/Qwen3.8-27B-MLX-4bit',
        'backend': 'cross-vendor',          # Apple Metal/MLX 与 NVIDIA CUDA/Triton 双后端
        'exact': True,                      # 逐字节对齐串行解码，回复带 token_sha
        'multi_node': True,                 # 双机 --tp 2
        'needs_container': True,            # 官方 CUDA 路径只给 NGC 容器
        'tier_affinity': ['cloud', 'edge', 'home'],
        'strengths': ['草稿只改速度不改字节（回复带 token_sha / min_rows，可自证）',
                      '同一份 4-bit 权重覆盖 Apple 与 NVIDIA 两条 ISA 路径',
                      'DGX Spark 上官方实测为 vLLM 的 1.6–3×'],
        'caveats': ['CUDA 路径依赖 NGC 容器；无 nvidia runtime 的机器跑不起来',
                    '仅支持 Qwen3.8-27B / Flash Next / GLM-5.3-Flash；'
                    'Nemotron 无 CUDA 引擎',
                    '拒绝 NVFP4 / GPTQ / AWQ 权重，必须用 MLX-4bit',
                    'HTTP 层无鉴权，必须绑回环',
                    'GLM 的 DFlash2 草稿模型为 CC BY-NC-ND，禁止商用'],
        'license': 'MIT（引擎）；模型权重各自许可',
    },
    'triton': {
        'label': 'NVIDIA Triton Inference Server（张量RT-LLM 后端）',
        'kind': 'openai-compat',            # Triton 的 /v2 API 之外有 OpenAI 兼容前端
        'default_base': 'http://127.0.0.1:8400/v1',
        'default_model': 'tensorrt-llm',
        'backend': 'nvidia-cuda',
        'exact': False,
        'multi_node': True,                 # Triton 支持 TP/PP 多卡编排
        'needs_container': True,            # 官方仅发 NGC 容器（nvcr.io/nvidia/tritonserver）
        'tier_affinity': ['cloud', 'edge'],
        'strengths': ['TensorRT-LLM 后端吞吐顶尖（FP8/FP4 内核）',
                      '集成的指标端点天然对接 Prometheus / DCGM',
                      '同一服务进程可托管多模型/多后端（python/onnx/tensorrt）'],
        'caveats': ['需 NGC 容器 + nvidia-container-toolkit，赛方节点未开放',
                    '模型需先经 TensorRT-LLM 编译为 engine，换模型要重编',
                    'OpenAI 兼容前端默认未开鉴权，必须绑回环'],
        'license': 'Apache-2.0（Server）；模型权重各自许可',
    },
    'nim': {
        'label': 'NVIDIA NIM 微服务（预置优化推理容器）',
        'kind': 'openai-compat',
        'default_base': 'http://127.0.0.1:8500/v1',
        'default_model': 'meta/llama-3.1-8b-instruct',
        'backend': 'nvidia-cuda',
        'exact': False,
        'multi_node': False,                # 单容器自包含，K8s 侧才做副本
        'needs_container': True,            # nvcr.io/nim/*，需 NGC API Key 拉取
        'tier_affinity': ['cloud', 'edge'],
        'strengths': ['开箱即用的优化推理（NVIDIA 预调 batch/内核），无需自行编译',
                      '标准 OpenAI 兼容 API，接入成本最低',
                      '企业级许可与更新通道（NVIDIA AI Enterprise）'],
        'caveats': ['拉取镜像需 NGC 账号与 API Key（离线赛点不可用）',
                    '镜像体积大（单模型 10GB+），首次启动慢',
                    '社区许可按模型而定，商用需确认 NIM 授权范围'],
        'license': 'NVIDIA AI Enterprise / 按模型各自许可',
    },
}

# 默认优先级（可复现 > 吞吐 > 便捷 > 托管 > 零依赖）。
# triton/nim 需要容器与 NGC 通道，赛点通常不满足，故排在 vllm 之后：
# 探活通过即插即用，不通过则被降级链自然跳过——这正是 HAL 的设计行为。
ORDER = ['tensorfold', 'vllm', 'triton', 'nim', 'ollama', 'mock']

SECRET_RANK = {'L1': 1, 'L2': 2, 'L3': 3, 'L4': 4}


# ---------------------------------------------------------------- 配置
def resolve(name=None):
    """决定当前生效的引擎名。"""
    n = (name or os.getenv('WNIDIA_ENGINE', 'auto')).strip().lower()
    if n and n != 'auto':
        return n if n in CATALOG else 'mock'
    mode = os.getenv('WNIDIA_MODE', 'auto').strip().lower()
    if mode == 'gpu':
        return os.getenv('WNIDIA_GPU_ENGINE', 'ollama').strip().lower()
    return 'mock'


def spec(name=None):
    n = resolve(name)
    e = dict(CATALOG[n])
    e['name'] = n
    e['base'] = os.getenv('VLLM_BASE') or e['default_base']
    e['model'] = os.getenv('VLLM_MODEL') or e['default_model']
    e['health_url'] = os.getenv('ENGINE_HEALTH_URL', '')
    return e


def catalog():
    return {k: dict(v, name=k) for k, v in CATALOG.items()}


# ---------------------------------------------------------------- 探活
def _base_host(base):
    b = (base or '').split('://', 1)[-1]
    return b.split('/', 1)[0].split(':', 1)[0]


def probe_openai_compat(base, timeout=2.5):
    """探活：先 /models，再 /health。返回结构化结果而不是抛异常。

    requests 延迟导入：本模块的**目录与选路**逻辑是纯计算，
    离线评估（engine-selector 的 evals）不应因为没装 requests 就跑不起来。
    """
    try:
        import requests
    except ImportError:
        return {'healthy': False, 'models': [], 'endpoint': '',
                'detail': '未安装 requests，无法探活（目录与选路仍可用）',
                'latency_ms': None}
    out = {'healthy': False, 'models': [], 'endpoint': '', 'detail': '',
           'latency_ms': None}
    try:
        compliance.assert_probe_allowed(_base_host(base), 'engine-probe')
    except compliance.ComplianceError as e:
        out['detail'] = f'合规拒绝：{e}'
        return out
    root = base[:-3] if base.endswith('/v1') else base.rstrip('/')
    t0 = time.time()
    for url, extract in ((f'{base}/models', 'models'), (f'{root}/health', 'health')):
        try:
            r = requests.get(url, timeout=timeout)
        except requests.RequestException as e:
            out['detail'] = f'{url} 不可达：{str(e)[:80]}'
            continue
        out['latency_ms'] = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            out['detail'] = f'{url} → HTTP {r.status_code}'
            continue
        out['endpoint'] = url
        out['healthy'] = True
        if extract == 'models':
            try:
                data = r.json().get('data', [])
                out['models'] = [d.get('id') for d in data if isinstance(d, dict)]
            except ValueError:
                pass
        out['detail'] = f'{url} → 200'
        break
    return out


def probe(name=None, base=None, timeout=2.5):
    s = spec(name)
    b = base or s['base']
    if s['kind'] == 'mock':
        return {'engine': s['name'], 'base': '(in-process)', 'model': s['model'],
                'healthy': True, 'models': [s['model']], 'endpoint': 'in-process',
                'detail': 'mock 引擎常驻可用', 'latency_ms': 0, 'backend': s['backend'],
                'exact': s['exact']}
    r = probe_openai_compat(b, timeout)
    r.update({'engine': s['name'], 'base': b, 'model': s['model'],
              'backend': s['backend'], 'exact': s['exact']})
    return r


def probe_all(timeout=2.0):
    out = {}
    for n in ORDER:
        out[n] = probe(n, timeout=timeout)
    return out


# ---------------------------------------------------------------- 选路（确定性排序）
def score(entry, task_type='chat', secret='L1', tier='edge',
          prefer_exact=False, healthy=True, real_available=True):
    """百分制确定性打分；同分按 ORDER 保证可复现。

    real_available：是否存在**探活通过的真实引擎**。
    只要有一台真实引擎可用，mock 就必须排到所有真实引擎之后——
    mock 是"精确但无真实 token"的占位，不能因为 exact=True 就压过真实推理，
    否则会出现"要求可复现时选了一个不产出 token 的引擎"这种荒谬结论。
    """
    s = 0.0
    reasons = []
    ranks = SECRET_RANK.get(secret, 1)
    need_exact = prefer_exact or ranks >= SECRET_RANK['L3']

    if entry['kind'] == 'mock':
        s -= 30; reasons.append('模拟引擎，无真实 token')
        if real_available:
            s -= 45; reasons.append('⚠ 已有可用真实引擎，mock 仅作最后兜底')

    if need_exact:
        if entry['exact']:
            s += 40; reasons.append('满足可复现/可举证要求（逐字节一致）')
        else:
            s -= 25; reasons.append('⚠ 不满足可复现要求，仅作降级项')
    else:
        if entry['exact']:
            s += 15; reasons.append('输出可复现（额外信任收益）')

    if entry['healthy']:
        s += 25; reasons.append('探活通过')
    else:
        s -= 60; reasons.append('⚠ 探活失败，进入降级链末尾')

    if tier in entry['tier_affinity']:
        s += 15; reasons.append(f'与 {tier} 档位亲和')

    if entry['needs_container']:
        s -= 12; reasons.append('需要容器运行时（无 nvidia runtime 时不可用）')

    if task_type in ('heavy', 'batch'):
        if entry['multi_node']:
            s += 12; reasons.append('支持多机张量并行，适合高吞吐批处理')
        if entry['name'] == 'vllm':
            s += 14; reasons.append('面向高吞吐批处理的生产级引擎（连续批处理成熟）')
        if entry['kind'] == 'mock':
            s -= 10
    else:
        if entry['exact'] and entry['name'] == 'tensorfold':
            s += 10; reasons.append('草稿加速且不改变输出，适合在线交互')

    return round(s, 1), reasons


def select(task_type='chat', secret='L1', tier='edge', prefer_exact=False,
           do_probe=True, timeout=2.0, allow=None):
    """返回 (选中引擎名, 结构化结论)。确定性、可解释、带降级链。"""
    allowed = [n for n in ORDER if n in (allow or ORDER)]
    probes = probe_all(timeout=timeout) if do_probe else {
        n: {'healthy': None, 'detail': '未探活（离线评估）'} for n in ORDER}

    def _healthy(n):
        h = probes.get(n, {}).get('healthy')
        return True if h is None else bool(h)

    real_available = any(_healthy(n) and CATALOG[n]['kind'] != 'mock'
                         for n in allowed)

    ranking = []
    for n in allowed:
        e = CATALOG[n]
        h = probes.get(n, {}).get('healthy')
        sc, reasons = score(dict(e, name=n, healthy=_healthy(n)), task_type,
                            secret, tier, prefer_exact, _healthy(n),
                            real_available)
        ranking.append({'engine': n, 'label': e['label'], 'score': sc,
                        'healthy': h, 'exact': e['exact'],
                        'backend': e['backend'],
                        'detail': probes.get(n, {}).get('detail', ''),
                        'reasons': reasons})
    # 同分排序：先看分数，再按 ORDER 固定次序 → 可复现
    ranking.sort(key=lambda r: (-r['score'], ORDER.index(r['engine'])))

    picked = ranking[0]['engine'] if ranking else 'mock'
    fallback = [r['engine'] for r in ranking[1:]]
    need_exact = prefer_exact or SECRET_RANK.get(secret, 1) >= SECRET_RANK['L3']
    guarantee = ('输出与串行解码逐字节一致，回复携带 token_sha / min_rows 可自证'
                 if CATALOG[picked]['exact']
                 else '不提供逐字节一致性保证：请勿用于需要举证的场景')
    return picked, {
        'selected': picked,
        'selected_label': CATALOG[picked]['label'],
        'score': ranking[0]['score'] if ranking else 0,
        'reason': ranking[0]['reasons'] if ranking else ['无可选项'],
        'ranking': ranking,
        'fallback_chain': fallback,
        'consistency_guarantee': guarantee,
        'consistency_required': bool(need_exact),
        'task_type': task_type, 'secret': secret, 'tier': tier,
        'probed': bool(do_probe),
        'notes': ('引擎一律只绑 127.0.0.1，对外由控制面 :9000 统一鉴权代理；'
                  '非映射端口的访问请走 SSH 隧道（手册第五章）。'),
    }


def bench_engine():
    """给 worker 用的兜底顺序：真实引擎优先，最后回落到 mock。"""
    return [n for n in ORDER if n != 'mock'] + ['mock']
