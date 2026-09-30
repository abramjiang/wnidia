# -*- coding: utf-8 -*-
"""计量与计费（BP P10「按 Token / 节点 / 席位算账分账」的补足）。

设计借鉴（非拷贝）：
- **OpenMeter**（Go，Apache-2.0，2.1k★）：把「用量事件（usage event）」与
  「账单维度（billing dimensions）」分开建模，聚合与结算解耦。
  本项目照此把 `meter_*`（采数）与 `price_*`（算钱）拆成两层。
- **FinOps FOCUS** 开放口径：账单条目带 service / resource / account 等维度，
  便于跨账本对账。本项目对应为 mode / engine / node / project / department 维度。

三层职责：
    meter_*  采集真实用量（优先采信引擎返回的 usage，缺失时估算并标 estimated）
    price_*  按口径折算金额
    record_* 落账（ledger）+ 维度汇总
"""
import os

from . import db, config

# 三口径单价（元）。默认值可用环境变量覆盖；仅用于演示口径，不代表报价。
PRICING = {
    # 按 token：元 / 百万 token
    'token': {'unit_cny_per_million': float(os.getenv('WNIDIA_PRICE_TOKEN', '2.0')),
              'label': '按 Token'},
    # 按节点：元 / 节点·小时
    'node': {'unit_cny_per_node_hour': float(os.getenv('WNIDIA_PRICE_NODE', '1.2')),
             'label': '按节点时长'},
    # 按席位：元 / 席位·天
    'seat': {'unit_cny_per_seat_day': float(os.getenv('WNIDIA_PRICE_SEAT', '8.0')),
             'label': '按席位'},
}

BILLING_MODES = tuple(PRICING)


def pricing_catalog():
    return {k: dict(v, mode=k) for k, v in PRICING.items()}


# ---------------------------------------------------------------- 采数
def estimate_tokens(prompt_chars, answer_chars):
    """无 usage 时的兜底估算。中文约 1.5 字/token，英文约 4 字符/token；
    这里用与网关既有口径一致的 `len//2`，并显式标 estimated。"""
    return max(1, int(prompt_chars) // 2), max(1, int(answer_chars) // 2)


def meter_from_usage(usage, prompt_chars=0, answer_chars=0):
    """把引擎返回的 usage 归一化为计量三元组。

    返回 {'prompt_tokens','completion_tokens','total_tokens','estimated'}
    - 引擎返回合法 usage → estimated=False（真实计量）
    - 缺失 / 非法 / 全 0 → 回退估算，estimated=True
    """
    est_in, est_out = estimate_tokens(prompt_chars, answer_chars)
    if isinstance(usage, dict):
        try:
            pt = int(usage.get('prompt_tokens') or 0)
            ct = int(usage.get('completion_tokens') or 0)
            tt = int(usage.get('total_tokens') or (pt + ct))
        except (TypeError, ValueError):
            pt = ct = tt = 0
        if tt > 0 and pt >= 0 and ct >= 0:
            return {'prompt_tokens': pt, 'completion_tokens': ct,
                    'total_tokens': tt, 'estimated': False}
    return {'prompt_tokens': est_in, 'completion_tokens': est_out,
            'total_tokens': est_in + est_out, 'estimated': True}


# ---------------------------------------------------------------- 算钱
def price(mode, *, tokens=0, node_seconds=0.0, seats=0):
    """按口径折算金额（元，保留 6 位小数避免累积误差）。"""
    m = mode if mode in PRICING else 'token'
    if m == 'token':
        amt = tokens / 1_000_000.0 * PRICING['token']['unit_cny_per_million']
    elif m == 'node':
        amt = node_seconds / 3600.0 * PRICING['node']['unit_cny_per_node_hour']
    else:
        amt = seats / 86400.0 * PRICING['seat']['unit_cny_per_seat_day']
    return round(amt, 6)


def credit_cost(tokens):
    """配额扣减用的整数积分（与 v3 口径保持兼容）。"""
    return max(1, int(tokens) // 128)


def default_mode():
    m = os.getenv('WNIDIA_BILLING_MODE', 'token').strip().lower()
    return m if m in PRICING else 'token'


# ---------------------------------------------------------------- 记账
def record(task, node_name, node_seconds=0.0, seats=0, note='settle',
           engine=None):
    """把一次完成的任务落账，返回账单条目。

    幂等由调用方保证（`harness._complete` 只调用一次）。
    """
    mode = task.billing_mode if task.billing_mode in PRICING else default_mode()
    tokens = int(task.prompt_tokens) + int(task.completion_tokens)
    amount = price(mode, tokens=tokens, node_seconds=node_seconds, seats=seats)
    db.add_ledger(
        tenant=task.tenant, task=task.task, node=node_name,
        cost=credit_cost(tokens or 1), note=note,
        node_seconds=node_seconds, tokens=tokens, seats=int(seats),
        billing_mode=mode, project=task.project or '',
        department=task.department or '',
        amount_cny=amount, estimated=bool(task.metering_estimated),
        engine=engine or task.engine or '')
    return {'task': task.task, 'tenant': task.tenant, 'mode': mode,
            'tokens': tokens, 'node_seconds': round(node_seconds, 3),
            'seats': int(seats), 'amount_cny': amount,
            'estimated': bool(task.metering_estimated),
            'project': task.project or '', 'department': task.department or ''}


# ---------------------------------------------------------------- 汇总（分账）
def summarize(rows=None, limit=2000):
    """把账本按租户 / 项目 / 部门 / 口径 / 引擎汇总。"""
    rows = rows if rows is not None else db.ledger_rows(limit)

    def _bump(bucket, key, r):
        b = bucket.setdefault(key, {'key': key, 'entries': 0, 'tokens': 0,
                                    'node_seconds': 0.0, 'seats': 0,
                                    'amount_cny': 0.0, 'estimated_entries': 0})
        b['entries'] += 1
        b['tokens'] += int(r.get('tokens') or 0)
        b['node_seconds'] += float(r.get('node_seconds') or 0)
        b['seats'] += int(r.get('seats') or 0)
        b['amount_cny'] = round(b['amount_cny'] + float(r.get('amount_cny') or 0), 6)
        if r.get('estimated'):
            b['estimated_entries'] += 1

    by_tenant, by_project, by_department, by_mode, by_engine = {}, {}, {}, {}, {}
    total = {'entries': 0, 'tokens': 0, 'node_seconds': 0.0, 'seats': 0,
             'amount_cny': 0.0, 'estimated_entries': 0}
    for r in rows:
        total['entries'] += 1
        total['tokens'] += int(r.get('tokens') or 0)
        total['node_seconds'] += float(r.get('node_seconds') or 0)
        total['seats'] += int(r.get('seats') or 0)
        total['amount_cny'] = round(
            total['amount_cny'] + float(r.get('amount_cny') or 0), 6)
        if r.get('estimated'):
            total['estimated_entries'] += 1
        _bump(by_tenant, r.get('tenant') or 'unknown', r)
        _bump(by_project, r.get('project') or '(未分项目)', r)
        _bump(by_department, r.get('department') or '(未分部门)', r)
        _bump(by_mode, r.get('billing_mode') or 'token', r)
        _bump(by_engine, r.get('engine') or '(未知引擎)', r)

    def _sort(d):
        return sorted(d.values(), key=lambda x: -x['amount_cny'])

    total['node_hours'] = round(total['node_seconds'] / 3600.0, 4)
    return {
        'total': total,
        'by_tenant': _sort(by_tenant),
        'by_project': _sort(by_project),
        'by_department': _sort(by_department),
        'by_billing_mode': _sort(by_mode),
        'by_engine': _sort(by_engine),
        'pricing': pricing_catalog(),
        'note': ('estimated_entries > 0 表示有账单条目来自估算而非引擎 usage；'
                 '对客户出账前应先消除。计费口径与单价见 docs/FINOPS.md。'),
    }


def metering_quality(rows=None, limit=2000):
    """计量质量：真实计量占比。用于说明"账单与算力一一对应"的达成度。"""
    rows = rows if rows is not None else db.ledger_rows(limit)
    est = sum(1 for r in rows if r.get('estimated'))
    n = len(rows)
    return {'entries': n, 'real': n - est, 'estimated': est,
            'real_ratio': round((n - est) / n, 4) if n else None}
