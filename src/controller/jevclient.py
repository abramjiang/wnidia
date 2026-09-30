# -*- coding: utf-8 -*-
"""Jev 决策客户端（System-1 决策增强层）。

定位：把“类型化问题 -> 校准概率”封装成三个高层决策：
  - screen_prompt : 入口护栏（注入 / 越权 / 隐私）
  - precheck_answer: 选择性校验（答案正确概率）
  - judge_answers : 多数决语义判定（等价 / 证据支持 / 标签）

默认模型已由 TypeSafe 商业 Jev（typesafe/jev-1.13）替换为开源高分开源实现
openJev-verdict-2.0（151M ModernBERT，非自回归，RLCD 校准；见 BENCHMARK §6 选型）。

三种模式（JEV_MODE）：
  off  : 关闭，所有高层决策返回 None（系统回退纯确定性逻辑）；
  mock : 本地离线评审引擎 local-offline（jev_offline）：L0 确定性归一化 + 多信号
         相似度，L1 本机 Ollama embedding（不可用自动回退 L0）；离线、可复算、
         输出可解释命中信号，无任何外部评审 API；
  live : 调用真实模型。按 JEV_BACKEND 选择后端：
         http  -> POST {JEV_BASE}/v1/systemone（TypeSafe / Laya / 自建 openJev 服务）；
         local -> 进程内 transformers 加载 openJev 权重。
  auto : 有 JEV_KEY 或 local 后端走 live，否则 mock。

任何真实后端异常都干净失败 -> None（回退确定性逻辑）。

安全原则：本模块只产出“建议 + 概率”，不直接改写任何安全门禁状态；
真实模型在中文（CJK）场景不被信任用于自动惩罚（交由确定性精确匹配）。
"""
import math
import os
import re
import time

import requests

from . import config
from . import jev_offline

# 外部端点使用独立会话：与内部 httpcli 不同，外部请求允许经过环境代理/网关。
_EXT = requests.Session()
_EXT.trust_env = True

# ---------------- 运行计数（看板/自检用） ----------------
C = {'calls': 0, 'mock': 0, 'live': 0, 'fallback': 0,
     'blocks': 0, 'skips': 0, 'adaptive_verify': 0}


def reset_counters():
    for k in C:
        C[k] = 0


def effective_backend():
    """返回真实模型后端：'local'（进程内 transformers）或 'http'（Jev 兼容 /v1/systemone）。"""
    b = (getattr(config, 'JEV_BACKEND', 'http') or 'http').lower()
    return 'local' if b == 'local' else 'http'


def effective_mode():
    m = (getattr(config, 'JEV_MODE', 'mock') or 'mock').lower()
    if m == 'auto':
        # local 后端无需外部密钥即可启用；http 后端需显式密钥（自建服务可设占位密钥）
        return 'live' if (getattr(config, 'JEV_KEY', '')
                          or effective_backend() == 'local') else 'mock'
    return m if m in ('off', 'mock', 'live') else 'mock'


def is_enabled():
    return effective_mode() != 'off'


def status():
    return {
        'enabled': is_enabled(),
        'mode': effective_mode(),
        'configured_mode': getattr(config, 'JEV_MODE', 'mock'),
        'backend': effective_backend(),
        'model': getattr(config, 'JEV_MODEL', 'heman10x/openJev-verdict-2.0'),
        'base': getattr(config, 'JEV_BASE', ''),
        'live_configured': bool(getattr(config, 'JEV_KEY', '')
                                or effective_backend() == 'local'),
        'offline': {
            'engine': 'local-offline',
            'embed_enabled': os.getenv('WNIDIA_JEV_EMBED', '1').strip().lower()
            in ('1', 'true', 'yes', 'on', 'y'),
            'embed_base': os.getenv('WNIDIA_JEV_EMBED_BASE',
                                   'http://127.0.0.1:11434'),
            'embed_model': os.getenv('WNIDIA_JEV_EMBED_MODEL',
                                    'nomic-embed-text'),
        },
        'counters': dict(C),
    }


def has_cjk(text):
    return any('\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff'
               for ch in (text or ''))


# ===================================================================
# 本地确定性启发式（mock）
# ===================================================================
_PUNCT = r'[\s\u3000，。！？、；：「」『』（）()【】\[\]{},.!?;:\'\"`~@#$%^&*\-_+=|\\/<>]+'


def _norm(text):
    return re.sub(_PUNCT, '', (text or '').lower())


def _tokens(text):
    if has_cjk(text):
        n = _norm(text)
        # 中文按字 + 相邻字二元切分
        grams = list(n) + [n[i:i + 2] for i in range(len(n) - 1)]
        return set(grams)
    return set(re.findall(r'[a-z0-9]+', (text or '').lower()))


def _jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _shape_conf(prob_map):
    """由概率分布形状推导置信度：越集中越高（0..1）。"""
    vals = list(prob_map.values())
    n = len(vals)
    if n == 0:
        return 0.0
    if n <= 2:
        # 二项：用距 0.5 的边距作为置信度（p=0.95 -> 0.9）
        return round(abs(2.0 * max(vals) - 1.0), 3)
    top = max(vals)
    ent = -sum(p * math.log(p + 1e-9) for p in vals) / math.log(n)
    return round(min(1.0, max(0.0, 0.5 * top + 0.5 * (1 - ent))), 3)


_ERROR_RE = re.compile(r'错误|不可用|无法|失败|error|unavailable|cannot|can not|'
                      r'fail|wrong|incorrect|异常|篡改', re.I)
# 强失败断言：一旦出现即表示答案在声称“做不到/不可用/错误”，优先于任何正向词
_FAILCLAIM_RE = re.compile(
    r'错误结果|不可用|无需|无法|不能|失败|不需要|unavailable|cannot|'
    r'can not|incorrect|wrong|not available|no gpu', re.I)
_GOOD_RE = re.compile(r'已完成|已把|分发|校验|回流|节点|调度|完成|done|complete|'
                     r'scheduled|verified|ok', re.I)

_INJECT_RE = re.compile(
    r'ignore\s+(all\s+)?(previous|prior|above)|disregard|'
    r'忽略(以上|之前|前面|上述)|无视(以上|之前)|system\s*prompt|'
    r'jailbreak|你现在是|从现在开始你是|act\s+as|developer mode|\bDAN\b', re.I)
_OVERREACH_RE = re.compile(
    r'\bsudo\b|\brm\s+-rf\b|删除所有|格式化|清空全部|提权|exploit|'
    r'绕过(限制|权限|安全)|越权|未经授权|privilege escalation', re.I)
_EMAIL_RE = re.compile(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}')
_PHONE_RE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
_IDCARD_RE = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')
_PRIV_RE = re.compile(r'身份证|信用卡|银行卡|密码|password|ssn|护照|住址|家庭住址', re.I)


def _mock_screen(prompt):
    p = prompt or ''
    inj = 0.95 if _INJECT_RE.search(p) else 0.04
    over = 0.93 if _OVERREACH_RE.search(p) else 0.03
    priv = 0.9 if (_EMAIL_RE.search(p) or _PHONE_RE.search(p)
                   or _IDCARD_RE.search(p) or _PRIV_RE.search(p)) else 0.05
    probs = {'injection': inj, 'overreach': over, 'privacy': priv}
    risk = round(max(inj, over, priv), 3)
    conf = _shape_conf({'risk': risk, 'safe': 1 - risk})
    reasons = [k for k, v in probs.items() if v >= 0.9]
    if risk >= 0.9 and conf >= getattr(config, 'JEV_CONF_AUTO', 0.85):
        verdict = 'block'
    elif risk >= 0.5:
        verdict = 'review'
    else:
        verdict = 'allow'
    return {**probs, 'risk': risk, 'confidence': conf,
            'verdict': verdict, 'reasons': reasons,
            'engine': 'local-offline'}


def _wellformed(answer):
    a = answer or ''
    if not a.strip():
        return 0.05
    if _FAILCLAIM_RE.search(a):
        return 0.1
    n = len(_norm(a))
    if n < 3:
        return 0.3
    if _GOOD_RE.search(a) or n >= 8:
        return 0.92
    return 0.6


def _mock_precheck(prompt, answer):
    p = _wellformed(answer)
    return {'p_correct': round(p, 3),
            'confidence': _shape_conf({'correct': p, 'wrong': 1 - p}),
            'engine': 'local-offline'}


def _equiv_prob(a, b):
    if _norm(a) == _norm(b) and _norm(a):
        return 0.98
    j = _jaccard(a, b)
    if j >= 0.8:
        p = 0.96
    elif j >= 0.5:
        p = 0.85
    elif j >= 0.3:
        p = 0.58
    else:
        p = 0.16
    return round(p, 3)


def _support_prob(prompt, answer):
    a = answer or ''
    if not a.strip():
        return 0.05
    if _FAILCLAIM_RE.search(a):
        return 0.1
    # 与 prompt 有内容呼应，或是结构完整的完成句
    if _jaccard(prompt, a) >= 0.15 or _GOOD_RE.search(a):
        return 0.9
    if len(_norm(a)) >= 8:
        return 0.7
    return 0.4


def _label(equiv, support, answer):
    if _FAILCLAIM_RE.search(answer or ''):
        return 'tampered'
    if equiv < 0.5 and support < 0.5:
        return 'off_topic'
    if support < 0.5:
        return 'hallucinated'
    return 'honest'


def _mock_judge(prompt, reference, candidates):
    items = {}
    sem_min = getattr(config, 'JEV_SEMANTIC_MIN', 0.8)
    for node, ans in candidates:
        cmp = jev_offline.compare(ans, reference)   # L0(+L1) 离线评审
        equiv = cmp['equivalent']
        support = _support_prob(prompt, ans)
        label = _label(equiv, support, ans)
        divergent = (equiv < sem_min or support < 0.5
                     or label in ('tampered', 'off_topic'))
        items[node] = {'equivalent': equiv, 'supported': round(support, 3),
                       'label': label, 'confidence': cmp['confidence'],
                       'divergent': bool(divergent),
                       'tier': cmp['tier'],
                       'signals': cmp['signals'],
                       'evidence': cmp['evidence']}
    consensus = all(not v['divergent'] for v in items.values())
    return {'trust': True, 'mode': 'mock', 'engine': 'local-offline',
            'reference': reference, 'items': items, 'consensus': consensus}


# ===================================================================
# 真实模型调用（live）：按后端分发到 http 或 local
# ===================================================================
def _call(state, questions):
    """把 (state, typed-questions) 交给真实模型，返回 {name: answer} 字典。

    http  -> 调用 Jev 兼容的 /v1/systemone 端点；
    local -> 进程内 transformers 加载 openJev 权重（缺依赖/权重则抛异常，由 _dispatch 兜底）。
    """
    if effective_backend() == 'local':
        return _local_call(state, questions)
    return _live_call(state, questions)


def _live_call(state, questions):
    base = (getattr(config, 'JEV_BASE', '') or '').rstrip('/')
    url = base + '/v1/systemone'
    headers = {'Content-Type': 'application/json'}
    key = getattr(config, 'JEV_KEY', '')
    if key:
        headers['Authorization'] = f'Bearer {key}'
    body = {'model': getattr(config, 'JEV_MODEL', 'heman10x/openJev-verdict-2.0'),
            'state': state, 'questions': questions}
    r = _EXT.post(url, json=body, headers=headers,
                  timeout=getattr(config, 'JEV_TIMEOUT', 8))
    r.raise_for_status()
    j = r.json()
    return j.get('answers', j.get('decisions', j))


def _local_call(state, questions):
    """进程内推理：懒加载开源 openJev 模型（首次调用才 import torch/transformers）。"""
    from . import jev_local
    return jev_local.decide(getattr(config, 'JEV_MODEL',
                                    'heman10x/openJev-verdict-2.0'),
                            state, questions)


def _p(ans, key, default=0.5):
    try:
        return float(ans.get(key, default))
    except (TypeError, ValueError):
        return default


def _conf_of(ans, probs=None):
    try:
        if 'confidence' in ans:
            return float(ans['confidence'])
    except (TypeError, ValueError):
        pass
    return _shape_conf(probs or {})


def _live_screen(prompt):
    q = {
        'injection': {'type': 'noul',
                      'instructions': 'Does the text attempt prompt injection '
                                      'or instruction override?'},
        'overreach': {'type': 'noul',
                      'instructions': 'Does the text request an unauthorized '
                                      'or destructive action?'},
        'privacy': {'type': 'noul',
                    'instructions': 'Does the text contain private personal data?'}}
    a = _call({'prompt': prompt}, q)
    inj = _p(a['injection'], 'noul')
    over = _p(a['overreach'], 'noul')
    priv = _p(a['privacy'], 'noul')
    risk = round(max(inj, over, priv), 3)
    conf = min(_conf_of(a['injection']), _conf_of(a['overreach']),
               _conf_of(a['privacy']))
    reasons = [k for k, v in (('injection', inj), ('overreach', over),
                              ('privacy', priv)) if v >= 0.9]
    if risk >= 0.9 and conf >= getattr(config, 'JEV_CONF_AUTO', 0.85):
        verdict = 'block'
    elif risk >= 0.5:
        verdict = 'review'
    else:
        verdict = 'allow'
    return {'injection': round(inj, 3), 'overreach': round(over, 3),
            'privacy': round(priv, 3), 'risk': risk, 'confidence': conf,
            'verdict': verdict, 'reasons': reasons}


def _live_precheck(prompt, answer):
    q = {'correct': {'type': 'noul',
                     'instructions': 'Does the answer correctly and completely '
                                     'address the prompt?'}}
    a = _call({'prompt': prompt, 'answer': answer}, q)
    p = _p(a['correct'], 'noul')
    return {'p_correct': round(p, 3),
            'confidence': _conf_of(a['correct'])}


def _live_judge(prompt, reference, candidates):
    state = {'prompt': prompt, 'reference': reference,
             'candidates': [{'node': n, 'answer': x} for n, x in candidates]}
    q = {}
    for i, (node, _ans) in enumerate(candidates):
        q[f'equiv_{i}'] = {'type': 'noul',
                           'instructions': f'Is candidate_{i} semantically '
                                           'equivalent to the reference?'}
        q[f'support_{i}'] = {'type': 'noul',
                             'instructions': f'Does candidate_{i} correctly '
                                             'answer the prompt?'}
        q[f'label_{i}'] = {'type': 'choice',
                           'options': ['honest', 'off_topic',
                                       'tampered', 'hallucinated']}
    a = _call(state, q)
    items = {}
    sem_min = getattr(config, 'JEV_SEMANTIC_MIN', 0.8)
    confs = []
    for i, (node, ans) in enumerate(candidates):
        equiv = _p(a[f'equiv_{i}'], 'noul')
        support = _p(a[f'support_{i}'], 'noul')
        lchoice = a[f'label_{i}']
        probs = lchoice.get('probabilities', {})
        label = lchoice.get('choice') or (max(probs, key=probs.get)
                                         if probs else 'honest')
        conf = _conf_of(lchoice, probs)
        confs.append(conf)
        divergent = (equiv < sem_min or support < 0.5
                     or label in ('tampered', 'off_topic'))
        items[node] = {'equivalent': round(equiv, 3),
                       'supported': round(support, 3), 'label': label,
                       'confidence': round(conf, 3),
                       'divergent': bool(divergent)}
    # CJK 场景：真实 Jev 不被信任用于自动惩罚。
    # 只要 prompt / reference / 任一候选答案含中日韩文字即视为 CJK 场景，
    # 避免“英文 prompt + 中文答案”绕过门控。
    cjk = (has_cjk(prompt) or has_cjk(reference)
           or any(has_cjk(x) for _, x in candidates))
    min_conf = min(confs) if confs else 0
    trust = (not cjk) and min_conf >= getattr(config, 'JEV_CONF_AUTO', 0.85)
    consensus = all(not v['divergent'] for v in items.values())
    return {'trust': bool(trust), 'mode': 'live', 'reference': reference,
            'items': items, 'consensus': consensus}


# ===================================================================
# 公共入口（统一 try/except，失败干净回退 None）
# ===================================================================
def _dispatch(kind, fn, *args):
    mode = effective_mode()
    if mode == 'off':
        return None
    C['calls'] += 1
    t0 = time.time()
    try:
        if mode == 'live':
            res = fn(*args)
            C['live'] += 1
        else:
            res = fn(*args)
            C['mock'] += 1
        res['_latency_ms'] = int((time.time() - t0) * 1000)
        return res
    except Exception:
        # 任何失败（端点不可达 / 超时 / 解析错误）都回退，由调用方走确定性逻辑
        C['fallback'] += 1
        return None


def screen_prompt(prompt):
    mode = effective_mode()
    return _dispatch('screen',
                     _live_screen if mode == 'live' else _mock_screen, prompt)


def precheck_answer(prompt, answer):
    mode = effective_mode()
    return _dispatch('precheck',
                     _live_precheck if mode == 'live' else _mock_precheck,
                     prompt, answer)


def judge_answers(prompt, reference, candidates):
    mode = effective_mode()
    return _dispatch('judge',
                     _live_judge if mode == 'live' else _mock_judge,
                     prompt, reference, candidates)
