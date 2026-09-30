# -*- coding: utf-8 -*-
"""Engine Selector：在多个推理引擎之间做**确定性、可解释**的选路。

离线优先：默认使用内置目录 + 可选的 `--engines-file` 探活快照，无需任何服务即可评估；
在线模式：`--ctrl` 会读取控制面的 /admin/engines，拿到真实探活结果与各节点引擎画像。

只做决策，不发请求、不改配置——与 smart-dispatcher 同样的"dry-run"边界。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 单一路径失败时回退到内置目录，保证 skill 在任何环境下都能跑
try:
    from controller import engines as _eng
    from controller import compliance as _comp
    ENGINE_CATALOG = _eng.CATALOG
    ORDER = _eng.ORDER
    _HAS_CORE = True
except Exception:                                    # noqa: BLE001
    _eng = _comp = None
    _HAS_CORE = False
    ORDER = ['tensorfold', 'vllm', 'triton', 'nim', 'ollama', 'mock']
    ENGINE_CATALOG = {}

SECRET_RANK = {'L1': 1, 'L2': 2, 'L3': 3, 'L4': 4}
BIT = {'L1': 1, 'L2': 2, 'L3': 3, 'L4': 4}


# ---------------------------------------------------------------- 数据来源
def load_snapshot(path, builtin):
    """从快照文件读 (catalog, probes)。

    快照格式（两个键都可选）：
        {"catalog": {...各引擎画像...}, "live": {...各引擎探活结果...}}
    只给 live 时，catalog 回落到内置目录（controller/engines.py）。
    """
    if not path:
        return dict(builtin), {}
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError('快照文件必须是 JSON 对象')
    catalog = raw.get('catalog') or dict(builtin)
    probes = raw.get('live') or raw.get('probes') or {}
    if not isinstance(probes, dict):
        raise ValueError('live/probes 必须是 JSON 对象')
    return catalog, probes


def fetch_from_ctrl(ctrl, token, timeout=10):
    import requests
    s = requests.Session(); s.trust_env = False
    r = s.get(f'{ctrl.rstrip("/")}/admin/engines',
              headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    return j.get('catalog', {}), j.get('live', {}) or {}, j.get('decision', {})


# ---------------------------------------------------------------- 打分
def score(entry, task_type, secret, tier, prefer_exact, healthy,
          real_available=True):
    """与 controller/engines.py 的打分保持一致（单一语义，两处实现同步）。"""
    ranks = SECRET_RANK.get(secret, 1)
    need_exact = prefer_exact or ranks >= 3
    s, reasons = 0.0, []
    if entry.get('kind') == 'mock':
        s -= 30; reasons.append('模拟引擎，无真实 token')
        if real_available:
            s -= 45; reasons.append('⚠ 已有可用真实引擎，mock 仅作最后兜底')
    if need_exact:
        if entry.get('exact'):
            s += 40; reasons.append('满足可复现/可举证要求（逐字节一致）')
        else:
            s -= 25; reasons.append('不满足可复现要求，仅作降级项')
    elif entry.get('exact'):
        s += 15; reasons.append('输出可复现（额外信任收益）')
    if healthy:
        s += 25; reasons.append('探活通过')
    else:
        s -= 60; reasons.append('探活失败，进入降级链末尾')
    if tier in (entry.get('tier_affinity') or []):
        s += 15; reasons.append(f'与 {tier} 档位亲和')
    if entry.get('needs_container'):
        s -= 12; reasons.append('需要容器运行时（无 nvidia runtime 时不可用）')
    if task_type in ('heavy', 'batch'):
        if entry.get('multi_node'):
            s += 12; reasons.append('支持多机张量并行，适合高吞吐批处理')
        if entry.get('name') == 'vllm':
            s += 14; reasons.append('面向高吞吐批处理的生产级引擎（连续批处理成熟）')
        if entry.get('kind') == 'mock':
            s -= 10
    elif entry.get('exact') and entry.get('name') == 'tensorfold':
        s += 10; reasons.append('草稿加速但不改变输出，适合在线交互')
    return round(s, 1), reasons


def decide(catalog, probes, task_type, secret, tier, prefer_exact, probed):
    order = [n for n in ORDER if n in catalog] or list(catalog)

    def _healthy(n):
        h = (probes.get(n) or {}).get('healthy')
        return True if h is None else bool(h)     # 未知按可用处理

    real_available = any(_healthy(n) and catalog[n].get('kind') != 'mock'
                         for n in order)
    ranking = []
    for n in order:
        e = dict(catalog[n]); e['name'] = n
        p = probes.get(n) or {}
        sc, reasons = score(e, task_type, secret, tier, prefer_exact,
                            _healthy(n), real_available)
        ranking.append({
            'engine': n, 'label': e.get('label', n), 'score': sc,
            'healthy': p.get('healthy'), 'exact': bool(e.get('exact')),
            'backend': e.get('backend', ''), 'kind': e.get('kind', ''),
            'detail': p.get('detail', '未探活'),
            'strengths': e.get('strengths', []),
            'caveats': e.get('caveats', []),
            'reasons': reasons,
        })
    ranking.sort(key=lambda r: (-r['score'], order.index(r['engine'])))

    if not ranking:
        return {'selected': None, 'score': 0, 'reason': ['目录为空'],
                'ranking': [], 'fallback_chain': []}

    picked = ranking[0]
    need_exact = prefer_exact or SECRET_RANK.get(secret, 1) >= 3
    exact = bool(catalog[picked['engine']].get('exact'))
    guarantee = ('输出与串行解码逐字节一致，回复携带 token_sha / min_rows 可自证'
                 if exact else
                 '不提供逐字节一致性保证：请勿用于需要举证的场景')
    return {
        'selected': picked['engine'],
        'selected_label': picked['label'],
        'score': picked['score'],
        'reason': picked['reasons'],
        'ranking': ranking,
        'fallback_chain': [r['engine'] for r in ranking[1:]],
        'consistency_guarantee': guarantee,
        'consistency_required': bool(need_exact),
        'consistency_satisfied': bool(exact) if need_exact else None,
        'task_type': task_type, 'secret': secret, 'tier': tier,
        'probed': bool(probed),
    }


def main():
    ap = argparse.ArgumentParser(description='引擎选路决策（dry-run）')
    ap.add_argument('--task-type', default='chat',
                    choices=['chat', 'heavy', 'batch', 'embed'])
    ap.add_argument('--secret', default='L1', choices=list(BIT))
    ap.add_argument('--tier', default='edge',
                    choices=['cloud', 'edge', 'home', 'cpu'])
    ap.add_argument('--prefer-exact', action='store_true')
    ap.add_argument('--engine-pref', default=None,
                    choices=['mock', 'ollama', 'vllm', 'tensorfold',
                             'triton', 'nim'],
                    help='点名希望使用的引擎（仅作提示，不绕过硬约束）')
    ap.add_argument('--engines-file', default=None)
    ap.add_argument('--engines-source', default=None,
                    help='简写：直接指向探活快照 JSON（与 --engines-file 等价）')
    ap.add_argument('--ctrl', default=os.getenv('CTRL', 'http://127.0.0.1:9000'))
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    ap.add_argument('--live', action='store_true',
                    help='强制从控制面取实时探活结果')
    a = ap.parse_args()

    src = a.engines_file or a.engines_source
    notes = []
    catalog, probes, source = None, {}, 'builtin'
    if a.live:
        if not a.token:
            notes.append('--live 需要 Token，已回落到离线目录')
        else:
            try:
                catalog, probes, _ = fetch_from_ctrl(a.ctrl, a.token)
                source = f'ctrl:{a.ctrl}'
            except Exception as e:                     # noqa: BLE001
                notes.append(f'控制面实时探活失败（{str(e)[:80]}），'
                             f'已回落到离线目录')
                catalog = None
    if not catalog:
        try:
            catalog, probes = load_snapshot(src, ENGINE_CATALOG)
            if source == 'builtin':
                source = f'file:{src}' if src else 'builtin:controller/engines.py'
        except Exception as e:                         # noqa: BLE001
            print(json.dumps({'error': f'加载引擎目录失败：{str(e)[:160]}',
                              'source': src or a.ctrl, 'selected': None},
                             ensure_ascii=False, indent=2))
            sys.exit(1)
    if not catalog:
        print(json.dumps({'error': '引擎目录为空：检查 controller/engines.py '
                                   '是否可用',
                          'source': source, 'selected': None},
                         ensure_ascii=False, indent=2))
        sys.exit(1)

    out = decide(catalog, probes, a.task_type, a.secret, a.tier,
                 a.prefer_exact, bool(probes))
    out['source'] = source
    out['catalog_size'] = len(catalog)
    out['notes'] = notes
    if a.engine_pref:
        # "点名某个引擎"只作为提示，不绕过健康与一致性硬约束
        out['requested_engine'] = a.engine_pref
        out['requested_honored'] = (out.get('selected') == a.engine_pref)
        if not out['requested_honored']:
            out['notes'] = notes + [
                f'未采用点名的 {a.engine_pref}：'
                f'它在当前排序中位列第 '
                f'{[r["engine"] for r in out["ranking"]].index(a.engine_pref) + 1}'
                f'（或被探活/一致性约束否决）']
    out['compliance'] = {
        'probe_scope': 'loopback-only',
        'note': ('引擎一律只绑 127.0.0.1，对外由控制面 :9000 统一鉴权代理；'
                 '非公网映射端口的访问请走 SSH 隧道（手册第五章）。'),
    }
    if not out.get('selected'):
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
