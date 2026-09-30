# -*- coding: utf-8 -*-
"""Gate Inspector：GO / NO-GO 门槛体检（BP P21）。

在线：读控制面 `/admin/gates`；离线：用 `--state-file` 传入快照（评测与沙盒用）。
产出每条门槛的实测值、目标、样本量与判定，并给出整体收缩建议。
**样本不足时不判 GO**（沿用 OpenSLO 的"目标必须带窗口与样本"原则）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

ACTION = {
    'go': '维持当前投入，按计划扩张',
    'watch': '维持观察，补足样本后再判',
    'insufficient': '样本不足：不得判 GO；先跑满窗口再复评',
    'no-go': '触发收缩线：暂停或收缩该业务线（BP P21 原文口径）',
}
GATE_ACTION = {
    'home_uptime': '家庭节点供给：不达标则降级为"离线异步专用网格"，不扩万节点',
    'spot_consistency': '抽检一致率不足：提高抽检密度并复检可疑节点',
    'task_match': '匹配率不足：先锁锚定任务方（LOI / 预付），不开放机主招募',
    'edge_unit_margin': '单位经济不达标：先做单台毛利转正，再批量出货',
    'sku_margin': '毛利低于阈值：该 SKU 不上线（BP P21 原文）',
}


def evaluate_offline(state):
    """离线体检：输入含 nodes / tasks / events / ledger 的状态快照。"""
    nodes = state.get('nodes') or []
    tasks = state.get('tasks') or []
    events = state.get('events') or []
    ledger = state.get('ledger') or []

    rows = []

    ups = []
    for n in nodes:
        try:
            ups.append(min(max(float(n.get('uptime_ratio') or 0), 0.0), 1.0))
        except (TypeError, ValueError):
            continue
    rows.append(_row('home_uptime', '内测节点可用率',
                     round(sum(ups) / len(ups), 4) if ups else None,
                     0.90, '>=', len(ups)))

    total = ok = 0
    for e in events:
        if e.get('kind') != 'verify':
            continue
        total += 1
        msg = e.get('message') or ''
        if '一致' in msg and '不一致' not in msg:
            ok += 1
    rows.append(_row('spot_consistency', '抽检一致率',
                     round(ok / total, 4) if total else None, 0.99, '>=', total))

    rows.append(_row('task_match', '任务匹配率',
                     round(sum(1 for t in tasks if t.get('state') == 'done')
                           / len(tasks), 4) if tasks else None,
                     0.60, '>=', len(tasks)))

    rev = sum(float(r.get('amount_cny') or 0) for r in ledger)
    rows.append(_row('edge_unit_margin', '单台边缘箱毛利',
                     None if not ledger else 0.0, 0.0, '>', len(ledger)))
    rows.append(_row('sku_margin', '综合毛利（含授权费）',
                     None if rev <= 0 else 0.0, 0.60, '>=', len(ledger)))

    overall, note = _overall(rows)
    return {'source': 'offline', 'overall': overall, 'gates': rows,
            'action': note,
            'note': ('离线体检：成本类指标需要控制面完整账本才能算出，'
                     '离线快照下仅能给样本量；请用 /admin/gates 取准确值。')}


def _row(gate, label, value, target, comparator, sample, min_sample=5):
    if value is None or sample < min_sample:
        return {'gate': gate, 'label': label, 'value': value, 'target': target,
                'comparator': comparator, 'sample': sample,
                'verdict': 'insufficient', 'passed': None,
                'reason': f'样本不足（{sample} < {min_sample}）',
                'action': GATE_ACTION.get(gate, '')}
    passed = value >= target if comparator == '>=' else value > target
    return {'gate': gate, 'label': label, 'value': value, 'target': target,
            'comparator': comparator, 'sample': sample,
            'verdict': 'go' if passed else 'no-go', 'passed': passed,
            'reason': '达标' if passed else '未达标',
            'action': GATE_ACTION.get(gate, '')}


def _overall(rows):
    if any(r['verdict'] == 'no-go' for r in rows):
        return 'no-go', ACTION['no-go']
    if any(r['verdict'] == 'insufficient' for r in rows):
        return 'insufficient', ACTION['insufficient']
    return 'go', ACTION['go']


def fetch(ctrl, token, timeout=15):
    import requests
    s = requests.Session(); s.trust_env = False
    r = s.get(f'{ctrl.rstrip("/")}/admin/gates',
              headers={'Authorization': f'Bearer {token}'}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description='GO / NO-GO 门槛体检')
    ap.add_argument('--ctrl', default=os.getenv('CTRL', 'http://127.0.0.1:9000'))
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    ap.add_argument('--state-file', default=None,
                    help='离线快照（含 nodes/tasks/events/ledger）')
    ap.add_argument('--live', action='store_true', help='强制走控制面')
    a = ap.parse_args()

    if not a.live and a.state_file:
        try:
            with open(a.state_file, encoding='utf-8') as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError('快照必须是 JSON 对象')
        except Exception as e:                        # noqa: BLE001
            print(json.dumps({'error': f'读取快照失败：{str(e)[:120]}'},
                             ensure_ascii=False, indent=2))
            sys.exit(1)
        out = evaluate_offline(state)
        out['actions'] = ACTION
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    try:
        out = fetch(a.ctrl, a.token)
        out['source'] = f'ctrl:{a.ctrl}'
        out['actions'] = ACTION
        out['per_gate_action'] = GATE_ACTION
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:                            # noqa: BLE001
        print(json.dumps({'error': f'控制面不可达：{str(e)[:120]}',
                          'hint': '可改用 --state-file 做离线体检'},
                         ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == '__main__':
    main()
