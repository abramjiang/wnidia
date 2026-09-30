# -*- coding: utf-8 -*-
"""WNIDIA 本地离线评审引擎（JEV local-offline）。

目标：在**不依赖任何外部评审模型 / 云 API** 的前提下，给出可复算、可解释的
语义等价判断。分两层：

  L0 —— 确定性文本归一化 + 多信号相似度（纯标准库，离线、确定性）：
        归一化：Unicode NFKC、全半角、大小写、空白、标点；
                百分比 / 内存 / 质量 / 长度 / 时间等数字-单位归一；
                本地同义词（内置 + 可选外部 JSON）规范。
        信号  ：exact / containment / jaccard / levenshtein / rouge_l /
                tf_cosine（可选 tfidf_cosine，需外部 IDF 语料）。
  L1 —— 本地 embedding（默认走本机 Ollama 的 nomic-embed-text，权重在本机）：
        向量余弦作为语义信号；Ollama 不可用 / 超时 / 未安装时**自动回退 L0**，
        主流程不报错。

对外主入口：compare(a, b) -> 等价概率 + 置信度 + 命中信号清单（evidence）。
任何 L1 / 外部 IDF 的异常都被就地吞掉并降级，绝不抛出到调度主循环。
"""
import json
import math
import os
import re
import threading
import unicodedata
from collections import Counter, OrderedDict

# ===============================================================
# 0. 基础工具
# ===============================================================
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff]')
_KEEP_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff a-z0-9]')


def has_cjk(text):
    return bool(_CJK_RE.search(text or ''))


# ---------------------------------------------------------------
# 中文数字解析（支持 0..99999；用于"数字+单位""百分之X"语境）
# ---------------------------------------------------------------
_CN_DIGIT = {'零': 0, '〇': 0, '○': 0, '一': 1, '二': 2, '两': 2, '三': 3,
             '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNIT = {'十': 10, '百': 100, '千': 1000, '万': 10000}


def cn_to_int(s):
    """把中文数字串转为 int；无法解析时返回 None。"""
    if not s:
        return None
    total = 0
    section = 0          # 当前"万"以下累计
    number = 0           # 当前待乘的个位数字
    seen = False
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            seen = True
            if unit == 10000:
                section = (section + (number or 0)) * 10000 if section or number \
                    else 10000
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return None
    if not seen:
        return None
    return total + section + number


def parse_number(tok):
    """阿拉伯/中文数字 token -> float；失败 None。"""
    if tok is None:
        return None
    t = tok.strip()
    if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', t):
        return float(t)
    if re.fullmatch(r'[零〇○一二两三四五六七八九十百千]+', t):
        v = cn_to_int(t)
        return float(v) if v is not None else None
    return None


_NUM = r'([0-9]+(?:\.[0-9]+)?|[零〇○一二两三四五六七八九十百千]+)'


def _fmt(v):
    """规整换算结果：整数去小数点。"""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return ('%.3f' % v).rstrip('0').rstrip('.')


# ===============================================================
# 1. L0 归一化
# ===============================================================
# 数字-单位归一表：(匹配用单位, 规范单位, 换算系数)。
# 顺序敏感：先匹配多字符 / 易混淆单位（内存 GB 在质量 g 之前）。
# 裸 'g' 留给质量（克）以消除 g/gb 歧义；内存语境统一用 gb/mib 等
_MEMORY = [('gib', 'mb', 1024.0), ('gb', 'mb', 1024.0),
           ('mib', 'mb', 1.0), ('mb', 'mb', 1.0),
           ('kib', 'mb', 1 / 1024.0), ('kb', 'mb', 1 / 1024.0),
           ('tib', 'mb', 1024.0 * 1024.0), ('tb', 'mb', 1024.0 * 1024.0)]
_MASS = [('kg', 'g', 1000.0), ('千克', 'g', 1000.0), ('公斤', 'g', 1000.0),
         ('g', 'g', 1.0), ('克', 'g', 1.0)]
_LENGTH = [('km', 'm', 1000.0), ('千米', 'm', 1000.0),
           ('m', 'm', 1.0), ('米', 'm', 1.0),
           ('cm', 'm', 0.01), ('厘米', 'm', 0.01),
           ('mm', 'm', 0.001), ('毫米', 'm', 0.001)]
_TIME = [('小时', 's', 3600.0), ('h', 's', 3600.0),
         ('分钟', 's', 60.0), ('min', 's', 60.0),
         ('秒', 's', 1.0)]

# 内置同义词（保守、领域相关；key/value 均为无空白、小写形态）。
# value 为空串表示删除（虚词停用词）；长 key 在 _synonym_table 中优先替换。
BUILTIN_SYNONYMS = {
    '计算节点': '节点', '算力节点': '节点', 'gpu卡': 'gpu',
    '最合适': '最适合', '匹配到': '匹配',
    '调度完成': '完成调度', '已完成调度': '完成调度',
    '推理服务': '引擎', '推理引擎': '引擎',
    '显存': '内存',
    # 连接词 / 介词归一
    '以及': '和', '与': '和', '按照': '按',
    # 虚词停用词（对调度/算力领域无语义贡献，删除以提升核心词占比）
    '已经': '', '正在': '', '已': '', '的': '', '了': '', '把': '',
    '将': '', '着': '', '吗': '', '呢': '', '吧': '', '啊': '',
    '个': '', '间': '', '对': '', '为': '', '只': '',
}

_SYN_CACHE = {'loaded': False, 'table': None}


def _synonym_table():
    """内置同义词 + 可选外部 JSON（WNIDIA_JEV_SYNONYM_FILE）。"""
    if _SYN_CACHE['loaded']:
        return _SYN_CACHE['table']
    table = dict(BUILTIN_SYNONYMS)
    path = os.getenv('WNIDIA_JEV_SYNONYM_FILE', '')
    if path and os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                ext = json.load(fh)
            if isinstance(ext, dict):
                table.update({str(k).strip(): str(v).strip()
                              for k, v in ext.items()})
        except (ValueError, OSError):
            pass
    # 长 key 优先替换，避免子串抢先
    table = dict(sorted(table.items(), key=lambda kv: -len(kv[0])))
    _SYN_CACHE.update(loaded=True, table=table)
    return table


def _convert_units(s):
    """百分比 + 内存/质量/长度/时间 的数字-单位归一（此时尚未去标点/空白）。"""
    # 百分比：百分之三十 / 30% / 30％ -> 30pct
    def _pct(m):
        v = parse_number(m.group(1))
        return ('%spct' % _fmt(v)) if v is not None else m.group(0)
    s = re.sub(r'百分之' + _NUM, _pct, s)
    s = re.sub(_NUM + r'\s*[%％]', _pct, s)

    def _make(table):
        def _sub(m):
            v = parse_number(m.group(1))
            unit = m.group(2).lower()
            if v is None:
                return m.group(0)
            for u, canonical, factor in table:
                if unit == u:
                    return _fmt(v * factor) + canonical
            return m.group(0)
        return _sub

    # 先内存再质量，规避 g/gb 冲突；单位后不紧跟字母，避免 m 误匹配 min、g 误匹配 gpu
    mem_alt = '|'.join(sorted({u for u, _, _ in _MEMORY}, key=len, reverse=True))
    s = re.sub(_NUM + r'\s*(' + mem_alt + r')(?![a-z])', _make(_MEMORY), s)
    mass_alt = '|'.join(sorted({u for u, _, _ in _MASS}, key=len, reverse=True))
    s = re.sub(_NUM + r'\s*(' + mass_alt + r')(?![a-z])', _make(_MASS), s)
    len_alt = '|'.join(sorted({u for u, _, _ in _LENGTH}, key=len, reverse=True))
    s = re.sub(_NUM + r'\s*(' + len_alt + r')(?![a-z])', _make(_LENGTH), s)
    time_alt = '|'.join(sorted({u for u, _, _ in _TIME}, key=len, reverse=True))
    s = re.sub(_NUM + r'\s*(' + time_alt + r')(?![a-z])', _make(_TIME), s)
    return s


def _cn_digit_fold(s):
    """安全地把紧邻量词/单位的单字中文数字转为阿拉伯（三节点->3节点）。

    只在后跟 个/节点/分钟/秒/米/克/% 时转换，避免误伤普通语素。
    """
    def _sub(m):
        return str(_CN_DIGIT[m.group(1)])
    return re.sub(r'([零〇○一二两三四五六七八九])(?=\s*(?:个|节点|分钟|秒|米|克|%|％))',
                  _sub, s)


def normalize(text):
    """返回紧凑规范串（保留 CJK / 字母 / 数字，其余删除）。"""
    if text is None:
        return ''
    if isinstance(text, bytes):                 # bytes 先按 UTF-8 解码
        text = text.decode('utf-8', 'ignore')
    if not isinstance(text, str):               # 数字/其它对象：安全转字符串
        text = str(text)
    if not text:
        return ''
    s = unicodedata.normalize('NFKC', text)
    s = s.lower()
    s = _convert_units(s)
    s = _cn_digit_fold(s)
    s = re.sub(r'\s+', '', s)                       # 删除全部空白
    for k, v in _synonym_table().items():          # 同义词规范
        if k:
            s = s.replace(k, v)
    s = ''.join(ch for ch in s if _KEEP_RE.match(ch))
    return s


# ---------------------------------------------------------------
# 切词：CJK 连续段做 字 + 相邻二元；拉丁/数字段做词
# token_list 保留重复（用于词频）；tokenize 去重（用于 Jaccard）
# ---------------------------------------------------------------
def token_list(norm):
    toks = []
    if not norm:
        return toks
    for run in re.findall(r'[\u4e00-\u9fff\u3040-\u30ff]+', norm):
        toks.extend(run)                                       # 单字（保留次数）
        toks.extend(run[i:i + 2] for i in range(len(run) - 1))  # 相邻二元
    toks.extend(re.findall(r'[a-z0-9]+', norm))
    return toks


def tokenize(norm):
    return set(token_list(norm))


def term_freq(norm):
    freq = {}
    for tok in token_list(norm):
        freq[tok] = freq.get(tok, 0) + 1
    return freq


# ===============================================================
# 2. L0 相似度信号
# ===============================================================
def _levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def levenshtein_ratio(a, b):
    if not a and not b:
        return 1.0
    m = max(len(a), len(b))
    return 1.0 - _levenshtein(a, b) / m if m else 1.0


def _lcs_len(a, b):
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            cur.append(max(prev[j], cur[j - 1],
                           prev[j - 1] + (ca == cb)))
        prev = cur
    return prev[-1]


def rouge_l(a, b):
    """字符级 LCS-F1。"""
    if not a or not b:
        return 0.0
    lcs = _lcs_len(a, b)
    if lcs == 0:
        return 0.0
    return 2.0 * lcs / (len(a) + len(b))


def jaccard(a, b):
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def containment_ratio(a, b):
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    return 0.0


def tf_cosine(a, b):
    fa, fb = term_freq(a), term_freq(b)
    if not fa or not fb:
        return 0.0
    small, large = (fa, fb) if len(fa) <= len(fb) else (fb, fa)
    dot = sum(w * large.get(k, 0) for k, w in small.items())
    na = math.sqrt(sum(w * w for w in fa.values()))
    nb = math.sqrt(sum(w * w for w in fb.values()))
    return dot / (na * nb) if na and nb else 0.0


# 可选 IDF 语料（set_idf 注入）；无则 tfidf 信号不可用
_IDF = {}


def set_idf(idf):
    _IDF.clear()
    if isinstance(idf, dict):
        _IDF.update({k: float(v) for k, v in idf.items()})


def tfidf_cosine(a, b):
    if not _IDF:
        return None
    fa, fb = term_freq(a), term_freq(b)
    if not fa or not fb:
        return 0.0

    def w(freq):
        return {k: v * _IDF.get(k, 0.0) for k, v in freq.items()}
    wa, wb = w(fa), w(fb)
    small, large = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    dot = sum(x * large.get(k, 0) for k, x in small.items())
    na = math.sqrt(sum(x * x for x in wa.values()))
    nb = math.sqrt(sum(x * x for x in wb.values()))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------
# 数字事实一致性（数字是硬事实：关键数字不同则不等价）
# ---------------------------------------------------------------
_NUMTOK_RE = re.compile(r'[0-9]+(?:mb|pct|s|m|g)?')


def number_facts(norm):
    """提取 数字(+规范单位) 的多重集合：131072mb / 30pct / 3600s / 裸数字。"""
    return Counter(_NUMTOK_RE.findall(norm or ''))


def number_consistency(a, b):
    """数字-单位多重集合 Jaccard；双方都无数字返回 None（不适用）。"""
    ca, cb = number_facts(a), number_facts(b)
    if not ca and not cb:
        return None
    inter = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return inter / union if union else 1.0


# ===============================================================
# 3. L1 本地 embedding（Ollama）
# ===============================================================
class Embedder:
    """通过本机 Ollama /api/embeddings 获取向量；权重在本机，非外部评审服务。"""

    def __init__(self, base, model, timeout=3.0):
        self.base = (base or '').rstrip('/')
        self.model = model
        self.timeout = timeout
        self._cache = OrderedDict()
        # 缓存上限（条目），防止长期运行内存无界；可由环境变量覆盖
        try:
            self._cache_max = max(64,
                                  int(os.getenv('WNIDIA_JEV_EMBED_CACHE', '2048')))
        except ValueError:
            self._cache_max = 2048
        self._dead = False

    def _post(self, url, payload):
        import requests
        r = requests.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def embed(self, text):
        if self._dead or not self.base:
            raise RuntimeError('embedder unavailable')
        key = text
        if key in self._cache:
            self._cache.move_to_end(key)            # LRU：最近使用置尾
            return self._cache[key]
        j = self._post(self.base + '/api/embeddings',
                       {'model': self.model, 'prompt': text})
        vec = j.get('embedding')
        if not vec:
            raise RuntimeError('empty embedding')
        self._cache[key] = vec
        while len(self._cache) > self._cache_max:   # 超上限淘汰最久未用
            self._cache.popitem(last=False)
        return vec

    def mark_dead(self):
        self._dead = True


_EMB = {'inst': None, 'dead': False}
_EMB_LOCK = threading.Lock()


def _get_embedder():
    """单例；构造/探测失败返回 None（-> 回退 L0）。线程安全（双重检查锁）。"""
    if _EMB['dead']:
        return None
    if _EMB['inst'] is not None:
        return _EMB['inst']
    with _EMB_LOCK:
        if _EMB['dead']:
            return None
        if _EMB['inst'] is not None:
            return _EMB['inst']
        enabled = os.getenv('WNIDIA_JEV_EMBED', '1').strip().lower() \
            in ('1', 'true', 'yes', 'on', 'y')
        if not enabled:
            _EMB['dead'] = True
            return None
        base = os.getenv('WNIDIA_JEV_EMBED_BASE', 'http://127.0.0.1:11434')
        model = os.getenv('WNIDIA_JEV_EMBED_MODEL', 'nomic-embed-text')
        try:
            timeout = float(os.getenv('WNIDIA_JEV_EMBED_TIMEOUT', '3'))
        except ValueError:
            timeout = 3.0
        inst = Embedder(base, model, timeout)
        # 健康探测：用一次极短 embed 验证 Ollama 与模型就绪
        try:
            inst.embed('ping')
        except Exception:
            _EMB['dead'] = True
            return None
        _EMB['inst'] = inst
        return inst


def _reset_embedder():
    """测试用：清空单例与熔断状态。"""
    cache = getattr(_EMB.get('inst'), '_cache', None)
    if cache is not None:
        cache.clear()
    _EMB.update(inst=None, dead=False)


def embedding_signal(a, b):
    """L1 向量余弦；不可用返回 None（调用方据此回退）。"""
    emb = _get_embedder()
    if emb is None:
        return None
    try:
        va, vb = emb.embed(a), emb.embed(b)
    except Exception:
        if emb is not None:
            emb.mark_dead()
        _EMB['dead'] = True
        return None
    if len(va) != len(vb) or not va:
        return None
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    return dot / (na * nb) if na and nb else 0.0


# ===============================================================
# 4. 融合 -> 等价概率 + 置信度 + 可解释依据
# ===============================================================
# 常规 L0 信号权重（exact 为特殊增益不参与；containment 仅在>0 时纳入）。
# 词序不敏感的集合/词频信号为主，字符序信号为辅；embed 存在时另占一份。
_W0 = {'tf_cosine': 0.26, 'jaccard': 0.16, 'rouge_l': 0.16,
       'levenshtein': 0.08, 'tfidf': 0.08, 'containment': 0.12,
       'number': 0.14}
_W_EMBED = 0.32
# 字符序 DP（levenshtein/rouge_l，O(n^2)）长度上限：超过则跳过这两个信号，
# 由词序不敏感的集合/词频信号承担，避免长文本卡顿。可由环境变量覆盖。
try:
    _CHAR_DP_MAX = max(64, int(os.getenv('WNIDIA_JEV_CHAR_DP_MAX', '600')))
except ValueError:
    _CHAR_DP_MAX = 600


def _confidence(values, p):
    """信号越一致、概率越靠两端 -> 置信越高。"""
    vals = [v for v in values if v is not None]
    margin = abs(2.0 * p - 1.0)
    if len(vals) <= 1:
        agreement = 1.0
    else:
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var)
        agreement = max(0.0, 1.0 - min(1.0, std * 2.0))
    return round(min(1.0, max(0.0, 0.6 * margin + 0.4 * agreement)), 3)


def compare(a, b):
    """主入口：返回 {equivalent, confidence, signals, tier, evidence}。"""
    na, nb = normalize(a), normalize(b)
    signals = {
        'exact': 1.0 if (na and na == nb) else 0.0,
        'containment': round(containment_ratio(na, nb), 3),
        'jaccard': round(jaccard(na, nb), 3),
        'tf_cosine': round(tf_cosine(na, nb), 3),
    }
    # 字符序 DP 仅在长度阈值内计算（O(n^2)，超长跳过）
    if max(len(na), len(nb)) <= _CHAR_DP_MAX:
        signals['levenshtein'] = round(levenshtein_ratio(na, nb), 3)
        signals['rouge_l'] = round(rouge_l(na, nb), 3)
    tfidf = tfidf_cosine(na, nb)
    if tfidf is not None:
        signals['tfidf'] = round(tfidf, 3)
    num = number_consistency(na, nb)
    if num is not None:
        signals['number'] = round(num, 3)

    emb = embedding_signal(a, b)         # L1（原文送入，保留完整语义）
    tier = 'L0'
    if emb is not None:
        signals['embed'] = round(emb, 3)
        tier = 'L0+L1'

    # 有效信号：tf/jac/rouge/lev 始终是真实证据（即使为0）；
    # tfidf 仅在语料可得、containment 仅在>0（=0 表示"不适用"而非0分证据）。
    active = {k: signals[k] for k in
              ('tf_cosine', 'jaccard', 'rouge_l', 'levenshtein') if k in signals}
    if 'tfidf' in signals:
        active['tfidf'] = signals['tfidf']
    if 'number' in signals:
        active['number'] = signals['number']
    if signals.get('containment', 0) > 0:
        active['containment'] = signals['containment']

    w = {k: _W0[k] for k in active if k in _W0}
    if emb is not None:
        w = {k: v * (1.0 - _W_EMBED) for k, v in w.items()}
        w['embed'] = _W_EMBED
        active['embed'] = signals['embed']
    wsum = sum(w.values()) or 1.0
    s = sum(active[k] * w[k] for k in w) / wsum      # 原始相似度（线性）

    # 概率校准：语义"等价概率"是相似度的非线性（logistic）函数——
    # 确定性答案词汇有限，0.6~0.7 的相似度通常已高度等价。
    try:
        mid = float(os.getenv('WNIDIA_JEV_CAL_MID', '0.45'))
        k = float(os.getenv('WNIDIA_JEV_CAL_K', '8'))
    except ValueError:
        mid, k = 0.45, 8.0
    p = 1.0 / (1.0 + math.exp(-k * (s - mid)))
    if signals['exact']:
        p = max(p, 0.98)
    if signals.get('number') is not None and signals['number'] == 0:
        p = min(p, 0.12)                  # 关键数字硬冲突：直接判不等价
    if not na or not nb:                  # 任一为空：不等价
        p = min(p, 0.05)
    p = round(min(1.0, max(0.0, p)), 3)

    conf = _confidence(list(signals.values()), p)
    evidence = ['%s=%.2f' % (k, v) for k, v in signals.items()
                if v >= 0.5]
    if not evidence:
        evidence = ['最高信号 %s=%.2f' % (max(signals, key=signals.get),
                                         max(signals.values()))]
    if emb is not None:
        evidence.append('L1 本地向量(Ollama %s)'
                        % os.getenv('WNIDIA_JEV_EMBED_MODEL',
                                    'nomic-embed-text'))
    return {'equivalent': p, 'confidence': conf, 'tier': tier,
            'signals': signals, 'evidence': evidence}
