# -*- coding: utf-8 -*-
"""GO / NO-GO 门槛度量（BP P21 的轻量化落地）。

BP P21 原文给了五条量化退出线，但当时全是"目标值*"。本模块把它们变成
**可计算、可复算、可查样本量**的实测指标，并给出 GO / WATCH / NO-GO / 样本不足 四档判定。

设计借鉴：**OpenSLO** 规范的「objective / target / window」三段式——
每条门槛都必须声明指标、目标值、统计窗口，且必须报出样本量，
样本不足时**不允许判 GO**（避免"用一次成功冒充 90 天达标"）。
"""
import os
import time

from . import db

# 成本模型（演示口径，环境变量可覆盖；不代表真实 BOM 与电价）
EDGE_BOX_CAPEX = float(os.getenv('WNIDIA_EDGE_BOX_CAPEX_CNY', '28000'))
EDGE_BOX_LIFE_Y = float(os.getenv('WNIDIA_EDGE_BOX_LIFE_YEARS', '3'))
EDGE_BOX_POWER_W = float(os.getenv('WNIDIA_EDGE_BOX_POWER_W', '100'))
ELEC_CNY_PER_KWH = float(os.getenv('WNIDIA_ELEC_CNY_PER_KWH', '0.7'))
VGPU_LICENSE_CNY_PER_YEAR = float(os.getenv('WNIDIA_VGPU_LICENSE_CNY', '0'))

MIN_SAMPLE = int(os.getenv('WNIDIA_GATE_MIN_SAMPLE', '5'))

# 门槛定义（对照 BP P21 表格逐条）
GATES = (
    {'id': 'home_uptime', 'bp': 'P21 家庭节点供给',
     'label': '内测节点可用率', 'metric': 'uptime_ratio',
     'target': 0.90, 'comparator': '>=', 'window': '内测窗口',
     'basis': '节点画像 uptime_ratio 加权均值（真机应由 DCGM/Prometheus 采样）'},
    {'id': 'spot_consistency', 'bp': 'P21 家庭节点可信度',
     'label': '抽检一致率', 'metric': 'spot_check_consistency',
     'target': 0.99, 'comparator': '>=', 'window': '累计',
     'basis': '多数决校验中"全员一致 / 语义等价一致"的占比'},
    {'id': 'task_match', 'bp': 'P21 双边市场冷启动',
     'label': '任务匹配率', 'metric': 'task_match_rate',
     'target': 0.60, 'comparator': '>=', 'window': '累计',
     'basis': '被执行（done）任务数 / 已提交任务数'},
    {'id': 'edge_unit_margin', 'bp': 'P21 边缘单位经济',
     'label': '单台边缘箱毛利', 'metric': 'edge_unit_gross_margin',
     'target': 0.0, 'comparator': '>', 'window': '测算窗口',
     'basis': f'收入 − 摊销({EDGE_BOX_LIFE_Y}年) − 电费，CAPEX {EDGE_BOX_CAPEX:.0f} 元'},
    {'id': 'sku_margin', 'bp': 'P21 vGPU 授权隐性成本',
     'label': '综合毛利（含授权费）', 'metric': 'sku_gross_margin',
     'target': 0.60, 'comparator': '>=', 'window': '测算窗口',
     'basis': f'扣减 vGPU 授权 {VGPU_LICENSE_CNY_PER_YEAR:.0f} 元/年后毛利'},
)


# ---------------------------------------------------------------- 指标计算
def _tasks():
    return db.all_tasks()


def m_uptime_ratio(nodes=None):
    nodes = nodes if nodes is not None else db.all_nodes()
    if not nodes:
        return None, 0
    vals = []
    for n in nodes:
        try:
            vals.append(min(max(float(n.uptime_ratio or 0.0), 0.0), 1.0))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 4), len(vals)


def m_spot_check_consistency(events=None):
    """一致率 = 一致的校验次数 / 总校验次数。样本来自 verify 类事件。"""
    evs = events if events is not None else db.recent_events(100000)
    total = ok = 0
    for e in evs:
        if e.kind != 'verify':
            continue
        total += 1
        if '一致' in e.message and '不一致' not in e.message:
            ok += 1
    if total == 0:
        return None, 0
    return round(ok / total, 4), total


def m_task_match_rate(tasks=None):
    ts = tasks if tasks is not None else _tasks()
    if not ts:
        return None, 0
    done = sum(1 for t in ts if t.state == 'done')
    return round(done / len(ts), 4), len(ts)


def m_edge_unit_gross_margin(window_s=None, tasks=None):
    """单台边缘箱毛利：用窗口内 edge 层收入对照 capex 摊销 + 电费。"""
    window_s = window_s or 30 * 86400
    now = int(time.time() * 1000)
    rows = db.ledger_between(now - int(window_s * 1000), now + 1)
    revenue = sum(float(r.get('amount_cny') or 0) for r in rows
                  if _is_edge(r.get('node')))
    hours = window_s / 3600.0
    amort = EDGE_BOX_CAPEX / (EDGE_BOX_LIFE_Y * 365 * 24) * hours
    power = EDGE_BOX_POWER_W / 1000.0 * hours * ELEC_CNY_PER_KWH
    cost = amort + power
    if cost <= 0:
        return None, 0
    return round((revenue - cost) / cost, 4), len(rows)


def m_sku_gross_margin(window_s=None):
    window_s = window_s or 30 * 86400
    now = int(time.time() * 1000)
    rows = db.ledger_between(now - int(window_s * 1000), now + 1)
    revenue = sum(float(r.get('amount_cny') or 0) for r in rows)
    hours = window_s / 3600.0
    amort = EDGE_BOX_CAPEX / (EDGE_BOX_LIFE_Y * 365 * 24) * hours
    power = EDGE_BOX_POWER_W / 1000.0 * hours * ELEC_CNY_PER_KWH
    lic = VGPU_LICENSE_CNY_PER_YEAR / (365 * 24) * hours
    cost = amort + power + lic
    if revenue <= 0:
        return None, len(rows)
    return round((revenue - cost) / revenue, 4), len(rows)


def _is_edge(node_name):
    if not node_name:
        return False
    n = db.get_node(node_name)
    return bool(n and n.tier == 'edge')


METRIC_FUNCS = {
    'uptime_ratio': m_uptime_ratio,
    'spot_check_consistency': m_spot_check_consistency,
    'task_match_rate': m_task_match_rate,
    'edge_unit_gross_margin': m_edge_unit_gross_margin,
    'sku_gross_margin': m_sku_gross_margin,
}


# ---------------------------------------------------------------- 判定
def _compare(value, target, comparator):
    if comparator == '>=':
        return value >= target
    if comparator == '>':
        return value > target
    if comparator == '<=':
        return value <= target
    return value < target


def evaluate(window_s=None, persist=True, min_sample=None):
    min_sample = MIN_SAMPLE if min_sample is None else int(min_sample)
    rows, go, nogo, watch, insufficient = [], 0, 0, 0, 0

    for g in GATES:
        fn = METRIC_FUNCS.get(g['metric'])
        value, sample = (None, 0)
        if fn:
            try:
                if g['metric'] in ('edge_unit_gross_margin', 'sku_gross_margin'):
                    value, sample = fn(window_s=window_s)
                else:
                    value, sample = fn()
            except TypeError:
                value, sample = fn()

        if value is None or sample < min_sample:
            verdict = 'insufficient'
            passed = None
            reason = (f'样本不足（{sample} < {min_sample}）或指标不可计算，'
                      f'按 OpenSLO 原则不得判 GO')
            insufficient += 1
        else:
            passed = _compare(value, g['target'], g['comparator'])
            if passed:
                verdict, reason = 'go', '达标'
                go += 1
            else:
                verdict = 'no-go'
                reason = f'未达标（{value} {g["comparator"]} {g["target"]} 不成立）'
                nogo += 1
        rows.append({
            'gate': g['id'], 'bp_ref': g['bp'], 'label': g['label'],
            'metric': g['metric'], 'value': value, 'target': g['target'],
            'comparator': g['comparator'], 'window': g['window'],
            'sample': sample, 'min_sample': min_sample,
            'passed': passed, 'verdict': verdict, 'reason': reason,
            'basis': g['basis'],
        })

    overall = ('no-go' if nogo else
               ('insufficient' if insufficient else
                ('watch' if watch else 'go')))
    summary = {
        'evaluated_at': int(time.time() * 1000),
        'overall': overall,
        'counts': {'go': go, 'no-go': nogo, 'insufficient': insufficient},
        'policy': ('任一门槛 NO-GO：收缩或暂停该业务线（BP P21 原文口径）；'
                   '样本不足不得判 GO。'),
        'gates': rows,
        'assumption_note': ('成本与单价为演示口径（可用环境变量覆盖），'
                            '非真实 BOM/电价/报价；真机应接 DCGM + Prometheus。'),
    }
    if persist:
        for r in rows:
            if r['value'] is None:
                continue
            db.add_gate_snapshot(r['gate'], r['value'], r['target'],
                                 r['comparator'], bool(r['passed']),
                                 r['verdict'], r['basis'])
    return summary


def status():
    latest = db.latest_gate_snapshots()
    return {'gates_defined': len(GATES), 'snapshots': latest,
            'thresholds': {'min_sample': MIN_SAMPLE,
                           'edge_box_capex_cny': EDGE_BOX_CAPEX,
                           'edge_box_life_years': EDGE_BOX_LIFE_Y,
                           'elec_cny_per_kwh': ELEC_CNY_PER_KWH,
                           'vgpu_license_cny_per_year': VGPU_LICENSE_CNY_PER_YEAR}}
