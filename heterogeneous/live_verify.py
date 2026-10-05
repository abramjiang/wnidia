# -*- coding: utf-8 -*-
"""真机验证 runner（P2 · 提分路径）—— 把"能跑"变成"有据可查的实测报告"。

## 为什么它决定评分

同类里功能远少于我们的项目排名却更高，原因不是功能，而是**工程可信度**：
它公开了"在三台真实机器上跑通某个模型"的实测结果。
评审看到的是证据，不是架构图。

本模块的目标：**一旦有真机，一条命令产出可公开的验证报告**，
且报告必须自带"这是实测还是模拟"的标记，杜绝把模拟当实测。

## 它测什么

| 指标 | 含义 |
|---|---|
| P50 / P95 时延 | 真实端到端时延分布（不是"决策质量"） |
| ok_rate | 成功率 |
| tokens/s | 实测吞吐（需上游返回 usage；否则标记 estimated） |
| 路由命中率 | 画像路由 vs 基线（首个健康/轮询）各自是否选中合理节点 |
| 成本 | 按画像计量的实测金额 |

## 诚实边界（关键）

1. **`transport` 可注入**。注入假传输层时，所有指标都会标记
   `simulated=True`，报告顶部会写明"模拟数据，不可对外"。
   真实验证**必须**使用默认真实 HTTP 传输层。
2. 上游不返回 usage 时，token 数按字符折算并标记 `estimated=True`，
   **不会伪造真实 token 数**。
3. 本模块不修改任何调度逻辑，只观测与记录。
"""

import json
import os
import statistics
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


DeviceProfile = None
DeviceRegistry = None
TaskFeature = None
route = None
meter = None


def _imp(mod: str, name: str):
    for path in ('controller.' + mod, mod):
        try:
            m = __import__(path, fromlist=[name])
            return getattr(m, name)
        except Exception:
            continue
    return None


DeviceProfile = _imp('device_profile', 'DeviceProfile')
DeviceRegistry = _imp('device_profile', 'DeviceRegistry')
TaskFeature = _imp('profile_routing', 'TaskFeature')
route = _imp('profile_routing', 'route')
meter = _imp('profile_metering', 'meter')

CHARS_PER_TOKEN = 3.0


# ---------------------------------------------------------------- 数据结构
@dataclass
class Probe:
    """一次探测的结果。"""
    node_id: str = ''
    ok: bool = False
    latency_ms: float = 0.0
    tokens: int = 0
    estimated: bool = False      # token 数是估算而非上游真实 usage
    error: str = ''

    def to_dict(self) -> Dict:
        return {'node_id': self.node_id, 'ok': self.ok,
                'latency_ms': round(self.latency_ms, 2), 'tokens': self.tokens,
                'estimated': self.estimated, 'error': self.error}


@dataclass
class Measure:
    """一组探测的统计结果。"""
    node_id: str = ''
    n: int = 0
    ok_rate: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    mean_ms: float = 0.0
    tokens_per_s: float = 0.0
    total_tokens: int = 0
    amount_cny: float = 0.0
    estimated: bool = False
    simulated: bool = False

    def to_dict(self) -> Dict:
        return {'node_id': self.node_id, 'n': self.n,
                'ok_rate': round(self.ok_rate, 4), 'p50_ms': round(self.p50_ms, 2),
                'p95_ms': round(self.p95_ms, 2), 'mean_ms': round(self.mean_ms, 2),
                'tokens_per_s': round(self.tokens_per_s, 3),
                'total_tokens': self.total_tokens,
                'amount_cny': round(self.amount_cny, 6),
                'estimated': self.estimated, 'simulated': self.simulated}


@dataclass
class VerifyTarget:
    """一个被验证的后端节点。"""
    node_id: str
    base_url: str = ''
    api_style: str = 'openai'

    def chat_url(self) -> str:
        b = (self.base_url or '').rstrip('/')
        return b + ('/api/chat' if self.api_style == 'ollama' else '/chat/completions')


# ---------------------------------------------------------------- 传输层
def real_transport(url: str, payload: Dict,
                   timeout: float = 60.0) -> Tuple[int, Dict]:
    """真实 HTTP 传输层（默认）。用于真机验证。"""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, {'_text': body}
    except urllib.error.HTTPError as e:
        return e.code, {'error': e.read().decode('utf-8', 'replace')[:200]}
    except Exception as e:
        return 0, {'error': repr(e)[:200]}


def is_real_transport(t: Callable) -> bool:
    """判断是否为真实传输层（决定报告是否标记 simulated）。"""
    return getattr(t, '__name__', '') == 'real_transport'


# ---------------------------------------------------------------- 探测
def _parse_usage(body: Dict, api_style: str, payload: Dict) -> Tuple[int, bool]:
    """解析 token 数；上游不给 usage 时按字符估算并标记 estimated。"""
    if api_style == 'ollama':
        n = int(body.get('eval_count', 0) or 0)
        if n > 0:
            return n, False
    else:
        u = body.get('usage') or {}
        n = int(u.get('completion_tokens', 0) or 0)
        if n > 0:
            return n, False
    text = ''
    if api_style == 'ollama':
        text = (body.get('message') or {}).get('content', '') or body.get('response', '')
    else:
        ch = body.get('choices') or []
        if ch:
            text = (ch[0].get('message', {}) or {}).get('content', '') or ch[0].get('text', '')
    return max(0, int(len(text) / CHARS_PER_TOKEN)), True


def probe_once(target: VerifyTarget, payload: Dict,
               transport: Callable = real_transport,
               timeout: float = 60.0) -> Probe:
    """对单个节点发一次请求并计时。"""
    t0 = time.time()
    code, body = transport(target.chat_url(), payload, timeout)
    dt = (time.time() - t0) * 1000.0
    p = Probe(node_id=target.node_id, latency_ms=dt)
    if code == 200:
        p.ok = True
        p.tokens, p.estimated = _parse_usage(body, target.api_style, payload)
    else:
        p.error = 'HTTP %s %s' % (code, str(body.get('error', ''))[:80])
    return p


def _pct(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    idx = min(len(s) - 1, int(round((len(s) - 1) * q)))
    return s[idx]


def measure(target: VerifyTarget, payload: Dict, n: int = 10,
            concurrency: int = 1, transport: Callable = real_transport,
            timeout: float = 60.0) -> Measure:
    """对单节点重复探测并统计。concurrency>1 时并发发送（模拟真实并发）。"""
    probes: List[Probe] = []
    if concurrency <= 1:
        probes = [probe_once(target, payload, transport, timeout)
                  for _ in range(max(1, n))]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(probe_once, target, payload, transport, timeout)
                    for _ in range(max(1, n))]
            probes = [f.result() for f in futs]

    oks = [p for p in probes if p.ok]
    lats = [p.latency_ms for p in oks] or [p.latency_ms for p in probes]
    total_tokens = sum(p.tokens for p in oks)
    total_s = sum(lats) / 1000.0

    m = Measure(node_id=target.node_id, n=len(probes),
                ok_rate=(len(oks) / len(probes)) if probes else 0.0,
                p50_ms=_pct(lats, 0.50), p95_ms=_pct(lats, 0.95),
                mean_ms=statistics.fmean(lats) if lats else 0.0,
                tokens_per_s=(total_tokens / total_s) if total_s > 0 else 0.0,
                total_tokens=total_tokens,
                estimated=any(p.estimated for p in oks),
                simulated=not is_real_transport(transport))

    if meter is not None:
        prof = None
        try:
            prof = _REGISTRY.get(target.node_id) if _REGISTRY else None
        except Exception:
            prof = None
        try:
            m.amount_cny = meter(prof, tokens=total_tokens,
                                 node_hours=total_s / 3600.0,
                                 estimated=m.estimated).total_amount_cny
        except Exception:
            m.amount_cny = 0.0
    return m


_REGISTRY: Any = None      # 供计量取画像（可选）


# ---------------------------------------------------------------- 场景矩阵
SCENARIOS = {
    'small-chat': {'model': 'qwen2.5:7b', 'context': 200, 'max_tokens': 128,
                   'concurrency': 1},
    'long-context': {'model': 'qwen2.5:7b', 'context': 8000, 'max_tokens': 128,
                     'concurrency': 1},
    'high-concurrency': {'model': 'qwen2.5:7b', 'context': 500,
                         'max_tokens': 512, 'concurrency': 8},
    'large-model': {'model': 'deepseek-r1:70b-q4', 'context': 500,
                    'max_tokens': 256, 'concurrency': 1},
}


def build_payload(scenario: Dict, api_style: str = 'openai') -> Dict:
    text = '测' * int(scenario.get('context', 200) * CHARS_PER_TOKEN)
    msgs = [{'role': 'user', 'content': text or 'ping'}]
    if api_style == 'ollama':
        return {'model': scenario.get('model', 'qwen2.5:7b'), 'messages': msgs,
                'stream': False,
                'options': {'num_predict': scenario.get('max_tokens', 128)}}
    return {'model': scenario.get('model', 'qwen2.5:7b'), 'messages': msgs,
            'max_tokens': scenario.get('max_tokens', 128), 'stream': False}


# ---------------------------------------------------------------- A/B
def ab_compare(targets: List[VerifyTarget], scenario_name: str = 'high-concurrency',
               n: int = 10, transport: Callable = real_transport,
               registry: Any = None, timeout: float = 60.0) -> Dict:
    """真实 A/B：基线（固定首节点） vs 画像路由。

    与 `profile_ab.py` 的区别：那个只评估**决策质量**（不真发请求），
    这里**真的发请求并测时延/吞吐**，因此只有接了真机才有意义。
    """
    if not targets:
        return {'ok': False, 'reason': '无目标节点'}
    sc = SCENARIOS.get(scenario_name, SCENARIOS['high-concurrency'])
    payload = build_payload(sc, targets[0].api_style)
    conc = int(sc.get('concurrency', 1))

    # 基线：固定第一个节点
    base = measure(targets[0], payload, n=n, concurrency=conc,
                   transport=transport, timeout=timeout)

    # 画像路由：按 TaskFeature 选节点；无画像时回退首个节点
    routed_node = targets[0]
    if registry is not None and route is not None and TaskFeature is not None:
        try:
            f = TaskFeature(
                model_size_gb=float(sc.get('model_size_gb', 0.0)),
                context_tokens=int(sc.get('context', 500)),
                concurrency=conc,
                phase=('decode-heavy' if int(sc.get('max_tokens', 0)) >= 512
                       or conc >= 8 else 'balanced'))
            r = route(f, registry)
            if r.get('ok') and r.get('node'):
                cand = [t for t in targets if t.node_id == r['node']]
                if cand:
                    routed_node = cand[0]
        except Exception:
            routed_node = targets[0]

    routed = measure(routed_node, payload, n=n, concurrency=conc,
                     transport=transport, timeout=timeout)

    delta_p95 = (routed.p95_ms - base.p95_ms)
    return {
        'ok': True,
        'scenario': scenario_name,
        'simulated': not is_real_transport(transport),
        'baseline': base.to_dict(),
        'routed': routed.to_dict(),
        'delta_p95_ms': round(delta_p95, 2),
        'delta_p95_pct': (round(delta_p95 / base.p95_ms * 100.0, 1)
                          if base.p95_ms > 0 else 0.0),
        'note': '负值=画像路由更快。模拟数据不可对外。',
    }


# ---------------------------------------------------------------- 报告
def run_matrix(targets: List[VerifyTarget], scenarios: Optional[List[str]] = None,
               n: int = 10, transport: Callable = real_transport,
               timeout: float = 60.0) -> Dict:
    """跑完整场景矩阵，返回结构化结果。"""
    names = scenarios or list(SCENARIOS.keys())
    out = {'simulated': not is_real_transport(transport),
           'generated_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
           'n_per_scenario': n, 'scenarios': {}}
    for name in names:
        sc = SCENARIOS.get(name, SCENARIOS['small-chat'])
        per_node = []
        for t in targets:
            payload = build_payload(sc, t.api_style)
            per_node.append(measure(t, payload, n=n,
                                    concurrency=int(sc.get('concurrency', 1)),
                                    transport=transport,
                                    timeout=timeout).to_dict())
        out['scenarios'][name] = {
            'config': sc,
            'nodes': per_node,
            'best_node': (min([x for x in per_node if x['ok_rate'] > 0],
                              key=lambda x: x['p95_ms'])['node_id']
                          if any(x['ok_rate'] > 0 for x in per_node) else ''),
        }
    return out


def report_md(result: Dict, ab: Optional[Dict] = None) -> str:
    """生成 Markdown 报告。**模拟数据会写在最顶部，防止被误当实测。**"""
    L = []
    sim = result.get('simulated')
    L.append('# WNIDIA 实测验证报告')
    L.append('')
    if sim:
        L.append('> ⚠️ **本报告为模拟数据（注入的假传输层），不可对外发布。**')
        L.append('> 真实验证请使用默认真实 HTTP 传输层（不注入 transport）。')
    else:
        L.append('> 数据来源：真实 HTTP 请求。')
    L.append('')
    L.append('- 生成时间：%s' % result.get('generated_at', ''))
    L.append('- 每场景请求数：%s' % result.get('n_per_scenario', 0))
    L.append('')
    L.append('## 场景矩阵')
    L.append('')
    L.append('| 场景 | 节点 | 成功率 | P50(ms) | P95(ms) | tokens/s | 估算 | 金额(¥) |')
    L.append('|---|---|---|---|---|---|---|---|')
    for name, blk in result.get('scenarios', {}).items():
        for nd in blk.get('nodes', []):
            L.append('| %s | %s | %.0f%% | %.1f | %.1f | %s | %s | %.6f |' % (
                name, nd['node_id'], nd['ok_rate'] * 100, nd['p50_ms'],
                nd['p95_ms'], nd['tokens_per_s'],
                '是' if nd['estimated'] else '否', nd['amount_cny']))
    if ab and ab.get('ok'):
        L.append('')
        L.append('## A/B 对比（%s）' % ab.get('scenario'))
        L.append('')
        L.append('| 口径 | 节点 | P95(ms) | tokens/s | 金额(¥) |')
        L.append('|---|---|---|---|---|')
        for k in ('baseline', 'routed'):
            d = ab[k]
            L.append('| %s | %s | %.1f | %s | %.6f |' % (
                '基线' if k == 'baseline' else '画像路由',
                d['node_id'], d['p95_ms'], d['tokens_per_s'], d['amount_cny']))
        L.append('')
        L.append('ΔP95 = %+.1f ms（%+.1f%%）—— %s'
                 % (ab['delta_p95_ms'], ab['delta_p95_pct'], ab['note']))
    L.append('')
    L.append('## 说明')
    L.append('')
    L.append('- "估算=是" 表示上游未返回 usage，token 数按字符折算，**非真实值**。')
    L.append('- 本报告只记录观测结果，不修改任何调度逻辑。')
    return '\n'.join(L) + '\n'


def save_report(md: str, path: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(md)
    return path


# ---------------------------------------------------------------- 自检
def _self_test() -> int:
    ok = 0
    fails = []

    def check(cond, msg):
        nonlocal ok
        if cond:
            ok += 1
            print('  ✅ %s' % msg)
        else:
            fails.append(msg)
            print('  ❌ %s' % msg)

    def fake(latency_ms=5.0, fail_nodes=()):
        """假传输层：固定时延，可指定失败节点（URL 需含节点名）。"""
        def t(url, payload, timeout=60.0):
            for n in fail_nodes:
                if n in url:
                    return 500, {'error': 'simulated'}
            time.sleep(latency_ms / 1000.0)
            if url.endswith('/api/chat'):
                return 200, {'message': {'role': 'assistant', 'content': '好的'},
                             'prompt_eval_count': 10, 'eval_count': 30}
            return 200, {'choices': [{'message': {'role': 'assistant',
                                                  'content': '好的'}}],
                         'usage': {'prompt_tokens': 10, 'completion_tokens': 30}}
        return t

    t1 = VerifyTarget('gb10-0', 'http://gb10-0:11434', 'ollama')
    t2 = VerifyTarget('5090-0', 'http://5090-0:8000/v1', 'openai')

    print('— 统计正确性 —')
    m = measure(t2, build_payload(SCENARIOS['small-chat']), n=5,
                transport=fake(10.0))
    check(m.n == 5 and m.ok_rate == 1.0, '5 次探测全部成功')
    check(8.0 <= m.p50_ms <= 60.0, 'P50 落在合理区间（%.1fms）' % m.p50_ms)
    check(m.p95_ms >= m.p50_ms, 'P95 ≥ P50（%.1f ≥ %.1f）' % (m.p95_ms, m.p50_ms))
    check(m.total_tokens == 150, 'token 汇总正确（5×30=150，实得 %d）'
          % m.total_tokens)
    check(m.tokens_per_s > 0, '吞吐可计算（%.1f tokens/s）' % m.tokens_per_s)
    check(m.simulated is True, '假传输层被正确标记为 simulated')

    print('— 估算标记 —')
    def fake_no_usage(url, payload, timeout=60.0):
        return 200, {'choices': [{'message': {'role': 'assistant',
                                              'content': 'x' * 60}}]}
    m2 = measure(t2, build_payload(SCENARIOS['small-chat']), n=1,
                 transport=fake_no_usage)
    check(m2.estimated is True,
          '上游无 usage → 标记 estimated（不伪造真实 token）')

    print('— 失败处理 —')
    m3 = measure(t1, build_payload(SCENARIOS['small-chat'], 'ollama'), n=3,
                 transport=fake(1.0, fail_nodes=('gb10-0',)))
    check(m3.ok_rate == 0.0 and m3.n == 3, '全部失败 → ok_rate=0 且不崩溃')

    print('— 场景矩阵 —')
    r = run_matrix([t1, t2], scenarios=['small-chat', 'high-concurrency'],
                   n=3, transport=fake(2.0))
    check(len(r['scenarios']) == 2, '矩阵覆盖 2 个场景')
    check(all(len(b['nodes']) == 2 for b in r['scenarios'].values()),
          '每个场景覆盖全部节点')
    check(all(b['best_node'] for b in r['scenarios'].values()),
          '每个场景都能给出最优节点')

    print('— A/B —')
    reg = None
    if DeviceRegistry is not None and DeviceProfile is not None:
        reg = DeviceRegistry()
        reg.register(DeviceProfile(node_id='gb10-0', gpu_name='GB10',
                                   family='soc', capacity_gb=128,
                                   bandwidth_gb_s=273, power_w=240))
        reg.register(DeviceProfile(node_id='5090-0', gpu_name='RTX 5090',
                                   family='consumer', capacity_gb=32,
                                   bandwidth_gb_s=1792, power_w=575))
    ab = ab_compare([t1, t2], scenario_name='high-concurrency', n=3,
                    transport=fake(2.0), registry=reg)
    check(ab.get('ok'), 'A/B 可执行')
    check(ab['baseline']['node_id'] != '' and ab['routed']['node_id'] != '',
          'A/B 双方均有节点（基线=%s / 路由=%s）'
          % (ab['baseline']['node_id'], ab['routed']['node_id']))
    check(ab['simulated'] is True, 'A/B 结果标记 simulated')

    print('— 报告 —')
    md = report_md(r, ab)
    check('模拟数据' in md, '报告顶部明确标注模拟（防误当实测）')
    check('| 场景 | 节点 |' in md and 'A/B 对比' in md, '报告含矩阵与 A/B 两张表')
    p = save_report(md, '/tmp/_wnidia_verify_report.md')
    check(os.path.exists(p) and os.path.getsize(p) > 100, '报告可落盘（%s）' % p)

    print('— 空输入 —')
    check(ab_compare([], transport=fake())['ok'] is False,
          '无目标节点 → 明确返回失败而非异常')

    total = ok + len(fails)
    print('\n自检: %s (%d/%d)' % ('ALL PASS' if not fails else 'HAS FAIL',
                                  ok, total))
    return 0 if not fails else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
