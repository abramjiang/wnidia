# -*- coding: utf-8 -*-
"""F4 · 租户 / 项目维度计量

现有 `metering.py` 是 **per-task** 的（一次调用一条记录），
无法回答私有云客户最关心的问题："**这个部门/项目这个月花了多少？哪台机器贡献的？**"

本模块在其之上做聚合：
    UsageRecord(tenant, project, node_id, tokens, node_hours, estimated)
        ↓
    aggregate() -> 按租户 / 项目 / 节点 的成本汇总

依赖 `profile_metering.meter` 计算单条成本（按设备画像差异化）。
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

try:
    from .profile_metering import meter
except ImportError:
    from profile_metering import meter


@dataclass
class UsageRecord:
    """一条用量记录（可由现有 metering 的 per-task 记录转换而来）。"""
    tenant: str = 'default'
    project: str = 'default'
    node_id: str = ''
    tokens: int = 0
    node_hours: float = 0.0
    estimated: bool = False       # 真实 usage vs 估算（沿用现有语义）

    def to_dict(self) -> Dict:
        return asdict(self)


def _key(t: str, p: str) -> str:
    return '%s/%s' % (t or 'default', p or 'default')


def aggregate(records: List[UsageRecord],
              profile_lookup=None) -> Dict:
    """按租户/项目聚合成本，并给出各节点贡献。

    profile_lookup: 可调用的 node_id -> DeviceProfile；为 None 时按未知设备计价
                    （成本系数回落 1.0，保守不低估）
    返回：
      {
        'by_tenant_project': {key: {tokens, hours, cost_cny, estimated_count, nodes:{}}},
        'by_node': {node_id: {tokens, hours, cost_cny}},
        'totals': {...}
      }
    """
    by_tp: Dict[str, Dict] = {}
    by_node: Dict[str, Dict] = {}

    total_tokens = 0
    total_hours = 0.0
    total_cost = 0.0
    est_count = 0

    for r in records:
        profile = None
        if profile_lookup:
            try:
                profile = profile_lookup(r.node_id)
            except Exception:
                profile = None

        m = meter(profile, tokens=r.tokens, node_hours=r.node_hours,
                  estimated=r.estimated)
        cost = m.total_amount_cny

        k = _key(r.tenant, r.project)
        e = by_tp.setdefault(k, {'tenant': r.tenant or 'default',
                                 'project': r.project or 'default',
                                 'tokens': 0, 'hours': 0.0, 'cost_cny': 0.0,
                                 'estimated_count': 0, 'nodes': {}})
        e['tokens'] += int(r.tokens)
        e['hours'] += float(r.node_hours)
        e['cost_cny'] += cost
        if r.estimated:
            e['estimated_count'] += 1
        nd = e['nodes'].setdefault(r.node_id or 'unknown',
                                   {'tokens': 0, 'hours': 0.0, 'cost_cny': 0.0})
        nd['tokens'] += int(r.tokens)
        nd['hours'] += float(r.node_hours)
        nd['cost_cny'] += cost

        n = by_node.setdefault(r.node_id or 'unknown',
                               {'tokens': 0, 'hours': 0.0, 'cost_cny': 0.0})
        n['tokens'] += int(r.tokens)
        n['hours'] += float(r.node_hours)
        n['cost_cny'] += cost

        total_tokens += int(r.tokens)
        total_hours += float(r.node_hours)
        total_cost += cost
        est_count += 1 if r.estimated else 0

    # 四舍五入，便于出账单
    for d in list(by_tp.values()) + list(by_node.values()):
        d['cost_cny'] = round(d['cost_cny'], 6)
        d['hours'] = round(d['hours'], 6)

    return {
        'by_tenant_project': by_tp,
        'by_node': by_node,
        'totals': {'tokens': total_tokens,
                   'hours': round(total_hours, 6),
                   'cost_cny': round(total_cost, 6),
                   'estimated_count': est_count,
                   'records': len(records)},
    }


def tenant_report(agg: Dict, tenant: str) -> Dict:
    """提取某租户的全部项目账单。"""
    items = {k: v for k, v in agg.get('by_tenant_project', {}).items()
             if v.get('tenant') == tenant}
    return {'tenant': tenant,
            'projects': items,
            'total_cost_cny': round(sum(v['cost_cny'] for v in items.values()), 6)}


def _self_test() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    # 无 profile_lookup：按未知设备计价（保守）
    recs = [
        UsageRecord(tenant='alice', project='rag', node_id='h100-0', tokens=1_000_000, node_hours=1.0),
        UsageRecord(tenant='alice', project='rag', node_id='gb10-0', tokens=1_000_000, node_hours=1.0),
        UsageRecord(tenant='bob', project='batch', node_id='h100-0', tokens=2_000_000, node_hours=2.0),
        UsageRecord(tenant='alice', project='dev', node_id='gb10-0', tokens=500_000, node_hours=0.5,
                    estimated=True),
    ]
    agg = aggregate(recs)

    check(agg['totals']['records'] == 4, '记录数正确')
    alice_rag = agg['by_tenant_project']['alice/rag']
    check(alice_rag['tokens'] == 2_000_000, '租户/项目 token 聚合正确')
    check(len(alice_rag['nodes']) == 2, '该项目的节点贡献含 2 台')
    check(alice_rag['cost_cny'] > 0, '成本已计算（%.4f）' % alice_rag['cost_cny'])

    rep = tenant_report(agg, 'alice')
    check(len(rep['projects']) == 2, 'alice 有 2 个项目')
    check(rep['total_cost_cny'] > 0, '租户总成本 %.4f' % rep['total_cost_cny'])

    check(agg['by_node']['h100-0']['tokens'] == 3_000_000, '节点维度聚合正确（h100）')

    # estimated 标记透传统计
    dev = agg['by_tenant_project']['alice/dev']
    check(dev['estimated_count'] == 1, 'estimated 记录被统计')

    # 空记录不崩溃
    empty = aggregate([])
    check(empty['totals']['records'] == 0 and empty['by_node'] == {},
          '空记录安全返回，不崩溃')

    # 带画像（不同设备成本不同）
    try:
        from device_profile import DeviceProfile
    except ImportError:
        from .device_profile import DeviceProfile
    reg = {'h100-0': DeviceProfile(node_id='h100-0', family='datacenter', power_w=700),
           'gb10-0': DeviceProfile(node_id='gb10-0', family='soc', power_w=240)}
    agg2 = aggregate(recs, profile_lookup=lambda nid: reg.get(nid))
    check(agg2['totals']['cost_cny'] > 0, '带画像聚合可用（%.4f）' % agg2['totals']['cost_cny'])
    # 同 token 下，GB10（系数 0.30 + 低能耗）应比 H100（1.0 + 700W）便宜
    check(agg2['by_node']['gb10-0']['cost_cny'] < agg2['by_node']['h100-0']['cost_cny'],
          '画像差异化生效：GB10 成本低于 H100')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
