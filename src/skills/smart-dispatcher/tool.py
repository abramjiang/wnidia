# -*- coding: utf-8 -*-
"""Smart Dispatcher：确定性的调度 dry-run，产出可解释决策。离线可用。"""
import argparse
import json
import os
import sys

import requests

S = requests.Session(); S.trust_env = False
SECRET_RANK = {'L1': 1, 'L2': 2, 'L3': 3, 'L4': 4}


def affinity_score(role, tier, task_type):
    if task_type in ('heavy', 'batch'):
        if role == 'prefill':
            return 40
        if tier == 'cloud':
            return 32
        if role == 'cpu':
            return 12
        return 20
    # chat / decode
    if role == 'decode':
        return 40
    if role == 'prefill':
        return 24
    if role == 'cpu':
        return 16
    return 20


def score_node(n, task):
    reasons = []
    a = affinity_score(n['role'], n['tier'], task.get('task_type', 'chat'))
    util = n.get('util', 0)
    u = round((100 - util) / 100 * 20, 1)
    need = max(task.get('need_mem_gb', 1), 0.1)
    m = round(min(n['free_mem_gb'] / need, 2) / 2 * 15, 1)
    kv = round(n.get('kv_hit', 0.5 if n['role'] == 'decode' else 0.2) * 10, 1)
    rep = round(n.get('reputation', 1) * 15, 1)
    total = round(a + u + m + kv + rep, 1)
    if n['role'] == 'decode' and task.get('task_type') == 'chat':
        reasons.append('decode 亲和')
    if util < 30:
        reasons.append('利用率余量充足')
    return total, reasons


def eligible(n, task):
    if n['status'] != 'online':
        return False, '节点离线'
    if n['free_mem_gb'] < task.get('need_mem_gb', 1):
        return False, '空闲显存不足'
    # 与真实 scheduler 对齐：L2+ 要求同主权区；L3+ 才要求可信节点
    rank = SECRET_RANK.get(task.get('secret', 'L1'), 1)
    region = task.get('sovereign_region', 'local')
    if rank >= SECRET_RANK['L2'] and n.get('sovereign_region') != region:
        return False, 'L2+ 数据主权不匹配'
    if rank >= SECRET_RANK['L3'] and not n.get('trusted'):
        return False, 'L3+ 机密任务要求可信节点'
    return True, 'ok'


def decide(task, state):
    candidates, rejected = [], []
    for n in state.get('nodes', []):
        ok, why = eligible(n, task)
        if not ok:
            rejected.append({'node': n['node'], 'reason': why}); continue
        sc, rs = score_node(n, task)
        candidates.append({'node': n['node'], 'score': sc, 'reasons': rs})
    candidates.sort(key=lambda c: c['score'], reverse=True)

    if not candidates:
        codes = {r['reason'] for r in rejected}
        admitted = 'sovereignty_violation' if any(
            '主权' in r['reason'] or '可信' in r['reason']
            for r in rejected) else 'no_capacity'
        return {'admitted': admitted, 'node': None, 'score': 0,
                'candidates': [], 'rejected': rejected,
                'preempt': {'needed': False, 'spot_task': None},
                'reasons': [f'无可用节点：{sorted(codes)}']}

    best = candidates[0]
    preempt = {'needed': False, 'spot_task': None}
    # S1 高优且最优节点正被 S3 spot 占用（在线状态里通过 running 任务体现）
    if task.get('sla') == 'sla-1':
        for t in state.get('tasks', []):
            if t.get('node') == best['node'] and t.get('state') == 'running' \
                    and t.get('sla') == 'sla-3':
                preempt = {'needed': True, 'spot_task': t.get('task')}
    return {
        'admitted': 'ok', 'node': best['node'], 'score': best['score'],
        'candidates': candidates, 'rejected': rejected, 'preempt': preempt,
        'reasons': best['reasons'] + [f"综合评分 {best['score']}，领先候选 "
                    f"{best['node']}"] +
                   ([f"需抢占 {preempt['spot_task']}"] if preempt['needed'] else [])
    }


def _load_state(a):
    """取集群状态：优先离线文件，否则打控制面。失败一律返回结构化错误。

    BUG-V5-03：原实现直接 `r.json()` 当状态用，控制面不可达或鉴权失败时
    会在下游抛出与真因无关的异常（甚至打出 traceback）。这里统一收口。
    """
    if a.state_file:
        try:
            with open(a.state_file, encoding='utf-8') as f:
                return json.load(f), None
        except (OSError, ValueError) as e:
            return None, f'读取状态快照失败：{str(e)[:120]}'
    try:
        r = S.get(f'{a.ctrl}/admin/state',
                  headers={'Authorization': f'Bearer {a.token}'}, timeout=15)
    except requests.RequestException as e:
        return None, f'控制面不可达（{a.ctrl}）：{str(e)[:120]}'
    if r.status_code != 200:
        return None, (f'控制面返回 {r.status_code}：'
                      f'通常是 Token 与 /admin/state 不匹配（{r.text[:80]}）')
    try:
        return r.json(), None
    except ValueError:
        return None, '控制面返回的不是 JSON'


def main():
    ap = argparse.ArgumentParser()
    # 注意：`--task` 会把 prompt 明文带进命令行，ps / /proc/*/cmdline 可见。
    # 调用方（agent/app.py）已改为写 0600 临时文件走 `--task-file`；
    # `--task` 仅保留给人工调试。
    ap.add_argument('--task', default='{}')
    ap.add_argument('--task-file', default=None)
    ap.add_argument('--state-file', default=None)
    ap.add_argument('--ctrl', default='http://127.0.0.1:9000')
    # BUG-V5-02：原先默认写死 'changeme'，而 Agent 早在 v3 就把 Token 从命令行
    # 移到环境变量了 —— 结果**通过 Agent 调用本 Skill 必然 401**，
    # 再拿错误响应当状态去决策。改为与其他 Skill 一致的 env 默认值。
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    a = ap.parse_args()
    try:
        task = json.load(open(a.task_file, encoding='utf-8')) if a.task_file \
            else json.loads(a.task)
    except (OSError, ValueError) as e:
        print(json.dumps({'error': f'任务参数解析失败：{str(e)[:120]}',
                          'admitted': None}, ensure_ascii=False, indent=2))
        sys.exit(1)
    state, err = _load_state(a)
    if err:
        print(json.dumps({'error': err, 'admitted': None, 'node': None},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(decide(task, state), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
