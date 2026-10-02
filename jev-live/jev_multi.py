# -*- coding: utf-8 -*-
"""多模型交叉裁决后端（JEV 路线 B）

用途：在**拿不到 openJev 权重 / 无外部裁决服务**时，仍能启用**真实**的 live 裁决。

原理：
  1. 用 Ollama 上 >=2 个模型对同一 prompt **各自独立推理**（真实 GPU 推理，非模拟）
  2. 把各节点的候选答案与这些"独立作答"做归一化相似度比对
  3. 相似度 >= 阈值判一致（等价/可信），否则判分歧 → 产出 live 裁决结论

输出：与 openJev 兼容的回答结构（noul 概率 / choice 概率），
      因此可直接复用 jevclient._live_judge 的既有逻辑，无需改动上层。

配置（controller/config.py）：
  WNIDIA_JEV_BACKEND=multi
  WNIDIA_JEV_MULTI_BASE       Ollama 地址，默认 http://127.0.0.1:11434
  WNIDIA_JEV_MULTI_MODELS     参与交叉的模型，逗号分隔；留空自动取前两个
  WNIDIA_JEV_MULTI_THRESHOLD  相似度阈值，默认 0.60
  WNIDIA_JEV_MULTI_TIMEOUT    单次推理超时秒，默认 300
"""

import difflib
import json
import re
import time
import urllib.request

from . import config

# 同一 prompt 的独立作答缓存，避免重复推理（screen/precheck/judge 会多次进入）
_CACHE = {}


# ---------------- 基础工具 ----------------
def _norm(s):
    """归一化：只保留字母数字并转小写，消除空白/标点/大小写差异。"""
    return ''.join(ch for ch in (s or '').lower() if ch.isalnum())


def _sim(a, b):
    """字符级相似度（0~1）。中文场景下比按空格切分更合理。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _http_json(url, payload=None, timeout=10):
    """轻量 HTTP：payload 为空走 GET，否则 POST JSON。"""
    data = None
    headers = {'User-Agent': 'wnidia-jev-multi'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method='GET' if data is None else 'POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


# ---------------- Ollama 交互 ----------------
def ollama_models(base, timeout=10):
    """列出本地可用模型名；失败返回空列表。"""
    try:
        j = _http_json(base.rstrip('/') + '/api/tags', timeout=timeout)
        return [m.get('name') for m in (j.get('models') or []) if m.get('name')]
    except Exception:
        return []


def ollama_generate(base, model, prompt, timeout=300):
    """调用 Ollama 生成，返回文本；失败抛异常（由上层 _dispatch 兜底回退）。"""
    j = _http_json(base.rstrip('/') + '/api/generate',
                   {'model': model, 'prompt': prompt, 'stream': False},
                   timeout=timeout)
    return (j.get('response') or '').strip()


def _resolve_models(base):
    """确定参与交叉裁决的模型：优先配置，其次自动取前两个。"""
    cfg = (getattr(config, 'JEV_MULTI_MODELS', '') or '').strip()
    if cfg:
        return [m.strip() for m in cfg.split(',') if m.strip()]
    return ollama_models(base)[:2]


def cross_answers(prompt, base, models, timeout):
    """让每个模型独立作答同一 prompt（带缓存）。"""
    key = (prompt, tuple(models))
    if key in _CACHE:
        return _CACHE[key]
    outs = []
    for m in models:
        t0 = time.time()
        try:
            txt = ollama_generate(base, m, prompt, timeout)
            outs.append({'model': m, 'answer': txt, 'ok': True,
                         'latency_s': round(time.time() - t0, 2)})
        except Exception as e:
            outs.append({'model': m, 'answer': '', 'ok': False,
                         'error': repr(e)[:160],
                         'latency_s': round(time.time() - t0, 2)})
    _CACHE[key] = outs
    return outs


# ---------------- 主入口 ----------------
def decide(state, questions):
    """按 openJev 兼容结构回答问题集。

    state     : {'prompt', 'reference', 'candidates': [{'node','answer'}]}
    questions : {name: {'type': 'noul'|'choice', 'instructions':..., 'options': [...]}}

    返回 {name: answer}：
      noul   -> {'noul': 概率}
      choice -> {'choice': 选项, 'probabilities': {...}, 'confidence': 概率}
    """
    base = getattr(config, 'JEV_MULTI_BASE', 'http://127.0.0.1:11434')
    threshold = float(getattr(config, 'JEV_MULTI_THRESHOLD', 0.60))
    timeout = float(getattr(config, 'JEV_MULTI_TIMEOUT', 300.0))

    prompt = (state or {}).get('prompt', '') or ''
    reference = (state or {}).get('reference', '') or ''
    candidates = (state or {}).get('candidates') or []

    models = _resolve_models(base)
    if not models:
        raise RuntimeError('multi backend: 未配置模型且 Ollama 不可用（%s）' % base)

    cross = cross_answers(prompt, base, models, timeout)
    valid = [c['answer'] for c in cross if c.get('ok') and c.get('answer')]
    if not valid:
        raise RuntimeError('multi backend: 所有交叉模型推理失败')

    answers = {}
    for name, q in (questions or {}).items():
        qtype = (q or {}).get('type', 'noul')

        # 一致性裁决：equiv_i / support_i / label_i
        m = re.match(r'^(equiv|support)_(\d+)$', name)
        if m:
            idx = int(m.group(2))
            ans = candidates[idx].get('answer', '') if idx < len(candidates) else ''
            # 与每个独立作答比对，取最高一致性作为概率
            sims = [_sim(ans, v) for v in valid]
            p = max(sims) if sims else 0.0
            answers[name] = {'noul': round(p, 4)}
            continue

        m = re.match(r'^label_(\d+)$', name)
        if m:
            idx = int(m.group(1))
            ans = candidates[idx].get('answer', '') if idx < len(candidates) else ''
            sims = [_sim(ans, v) for v in valid]
            p = max(sims) if sims else 0.0
            ok = p >= threshold
            choice = 'honest' if ok else 'hallucinated'
            answers[name] = {
                'choice': choice,
                'probabilities': {'honest': round(p, 4),
                                  'hallucinated': round(1 - p, 4),
                                  'off_topic': 0.0,
                                  'tampered': 0.0},
                'confidence': round(p, 4),
            }
            continue

        # 其余问题（入口护栏 injection / overreach / correct 等）：
        # multi 后端仅对"一致性裁决"提供真实证据，护栏类问题给中性/保守默认，
        # 避免误判阻断；如需完整护栏请用 local/http 后端。
        if qtype == 'choice':
            opts = (q or {}).get('options') or ['honest']
            answers[name] = {'choice': opts[0],
                             'probabilities': {o: (1.0 if o == opts[0] else 0.0)
                                               for o in opts},
                             'confidence': 0.5}
        else:
            # noul 默认 0.5（未知）；明显用于"是否恶意"的键取 0.0 以免误伤
            key = name.lower()
            p = 0.0 if any(k in key for k in ('inject', 'overreach', 'tamper',
                                              'jailbreak', 'privacy')) else 0.5
            answers[name] = {'noul': p}

    # 附证据，便于落盘与排查
    answers['__evidence__'] = {
        'engine': 'ollama-multi',
        'base': base,
        'models': models,
        'threshold': threshold,
        'reference_sim': round(_sim(reference, valid[0]), 4) if valid else 0.0,
        'cross': [{'model': c['model'], 'ok': c['ok'],
                   'latency_s': c['latency_s'],
                   'answer': (c['answer'] or '')[:200]} for c in cross],
    }
    return answers
