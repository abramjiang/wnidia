# -*- coding: utf-8 -*-
"""机主贡献结算（BP P7「按有效算力与在线时长结算，机主获分成、平台抽佣」）。

分成比例是 BP 里的**规划假设**（原文标注「分成比例为规划假设，需小规模实测标定*」），
本模块把比例做成可配置参数并在输出里显式标注 `assumption=True`，
避免把假设当成已定事实。

有效算力口径（本模块定义，可在参数中调整）：
    有效算力单元 = 实际消耗 token × 稳定性系数(stability) × 在线率系数(uptime_ratio)
在线时长直接取心跳累计（由节点画像的 uptime_ratio × 观察窗口折算）。
"""
import os
import time

from . import db, metering

# 分成与抽佣（规划假设，非已签约比例）
OWNER_SHARE = float(os.getenv('WNIDIA_OWNER_SHARE', '0.60'))
PLATFORM_FEE = round(1.0 - OWNER_SHARE, 6)

SETTLEMENT_NOTE = ('分成与抽佣比例为规划假设（BP 原文标注需小范围实测标定），'
                   '非已签约比例；上线前需以小规模实测替换。')


def _stability_factor(node):
    """稳定性系数：0.5 ~ 1.0。掉线多、时延抖动的节点折价。"""
    try:
        st = float(getattr(node, 'stability', 1.0) or 1.0)
        up = float(getattr(node, 'uptime_ratio', 1.0) or 1.0)
    except (TypeError, ValueError):
        st, up = 1.0, 1.0
    st = min(max(st, 0.0), 1.0)
    up = min(max(up, 0.0), 1.0)
    return round(0.5 + 0.5 * st * up, 4)


def compute(period=None, nodes=None, window_s=None, owner_share=None):
    """计算各节点在本期的结算明细（不落库，纯计算，便于沙盒反复验算）。"""
    nodes = nodes if nodes is not None else db.all_nodes()
    share = OWNER_SHARE if owner_share is None else float(owner_share)
    share = min(max(share, 0.0), 1.0)
    now_ms = int(time.time() * 1000)
    window_ms = int((window_s or 24 * 3600) * 1000)
    start = now_ms - window_ms
    rows = db.ledger_between(start, now_ms + 1)

    by_node = {}
    for r in rows:
        n = r.get('node') or '(未绑定)'
        b = by_node.setdefault(n, {'tokens': 0, 'node_seconds': 0.0,
                                   'amount_cny': 0.0, 'entries': 0})
        b['tokens'] += int(r.get('tokens') or 0)
        b['node_seconds'] += float(r.get('node_seconds') or 0)
        b['amount_cny'] = round(b['amount_cny'] + float(r.get('amount_cny') or 0), 6)
        b['entries'] += 1

    out = []
    for n in nodes:
        b = by_node.get(n.node)
        if not b:
            continue
        factor = _stability_factor(n)
        effective = round(b['tokens'] * factor, 2)
        # 在线时长：用画像在线率 × 观察窗口折算（沙盒口径）
        try:
            up = min(max(float(getattr(n, 'uptime_ratio', 1.0) or 1.0), 0.0), 1.0)
        except (TypeError, ValueError):
            up = 1.0
        online_hours = round((window_s or 86400) * up / 3600.0, 3)
        gross = round(b['amount_cny'], 6)
        owner = round(gross * share, 6)
        fee = round(gross - owner, 6)
        out.append({
            'node': n.node, 'owner': _owner_of(n), 'tier': n.tier,
            'layer': n.layer, 'entries': b['entries'],
            'tokens': b['tokens'], 'node_seconds': round(b['node_seconds'], 2),
            'stability_factor': factor, 'effective_units': effective,
            'online_hours': online_hours,
            'gross_cny': gross, 'owner_share_cny': owner,
            'platform_fee_cny': fee,
        })
    out.sort(key=lambda x: -x['gross_cny'])
    total = {
        'nodes': len(out),
        'gross_cny': round(sum(x['gross_cny'] for x in out), 6),
        'owner_share_cny': round(sum(x['owner_share_cny'] for x in out), 6),
        'platform_fee_cny': round(sum(x['platform_fee_cny'] for x in out), 6),
        'tokens': sum(x['tokens'] for x in out),
    }
    return {'period': period or time.strftime('%Y-%m-%d'),
            'owner_share_ratio': share, 'platform_fee_ratio': round(1 - share, 6),
            'assumption': True, 'note': SETTLEMENT_NOTE,
            'items': out, 'total': total}


def _owner_of(node):
    """机主标识：环境变量可指定，默认取节点名前缀（演示用）。"""
    mapping = os.getenv('WNIDIA_OWNERS', '')
    for pair in mapping.split(','):
        if ':' in pair:
            k, v = pair.split(':', 1)
            if k.strip() == node.node:
                return v.strip()
    return f'owner-of-{node.node}'


def settle(period=None, window_s=None, persist=True):
    """计算并落库（**幂等**：同一 period 先清后写，重算即覆盖，不叠加）。

    BUG-V4-18：原实现的 docstring 写着"同一 period + node 先清后写"，但代码里
    只有 `db.add_settlement()`、**没有任何清理步骤** —— 同一天连点两次结算，
    账面机主分成直接翻倍。这是钱的问题，不能只靠文档承诺，必须有对应动作。
    """
    res = compute(period=period, window_s=window_s)
    if persist:
        removed = db.clear_settlements(res['period'])   # 先清：同周期旧记录作废
        for it in res['items']:
            db.add_settlement(res['period'], it['node'], it['owner'],
                              it['effective_units'], it['online_hours'],
                              it['gross_cny'], it['owner_share_cny'],
                              it['platform_fee_cny'], note='auto')
            from . import trust
            trust.seal('settlement', f"{res['period']}:{it['node']}",
                       f"{it['effective_units']}|{it['owner_share_cny']}")
        db.add_event('settle',
                     f"结算完成：{res['period']} 共 {res['total']['nodes']} 个节点，"
                     f"机主分成 {res['total']['owner_share_cny']} 元，"
                     f"平台抽佣 {res['total']['platform_fee_cny']} 元"
                     f"（覆盖同周期旧记录 {removed} 条）")
        res['overwritten_records'] = removed
    return res


def status(limit=200):
    rows = db.settlement_rows(limit)
    total_owner = round(sum(float(r['owner_share_cny'] or 0) for r in rows), 6)
    total_fee = round(sum(float(r['platform_fee_cny'] or 0) for r in rows), 6)
    return {'records': len(rows), 'owner_share_cny': total_owner,
            'platform_fee_cny': total_fee,
            'owner_share_ratio': OWNER_SHARE, 'assumption': True,
            'note': SETTLEMENT_NOTE, 'recent': rows[:20],
            'pricing': metering.pricing_catalog()}
