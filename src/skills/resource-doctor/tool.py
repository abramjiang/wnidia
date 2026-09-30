# -*- coding: utf-8 -*-
"""Resource Doctor：读取控制面状态，产出结构化诊断报告。只读。"""
import argparse
import json
import os
import sys
import requests

S = requests.Session(); S.trust_env = False


def fetch_state(ctrl, token):
    r = S.get(f'{ctrl}/admin/state',
              headers={'Authorization': f'Bearer {token}'}, timeout=15)
    r.raise_for_status()
    return r.json()


def diagnose(state):
    # 字段容错：外部快照/半成品节点对象不应导致 KeyError 直接崩掉诊断
    def g(n, k, d=0):
        v = n.get(k, d)
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    nodes = state.get('nodes', []) or []
    online = [n for n in nodes if n.get('status') == 'online']
    lost = [n for n in nodes if n.get('status') != 'online']
    avg_util = round(sum(g(n, 'util') for n in online) / len(online), 1) \
        if online else 0.0
    low_mem = [n.get('node') for n in online if g(n, 'free_mem_gb', 99) < 1.0]

    findings, recs = [], []
    for n in lost:
        findings.append({'severity': 'high', 'code': 'node_lost',
                         'message': f"{n.get('node')} 状态={n.get('status')}，"
                                    f"已不在服务"})
        recs.append(f"将 {n.get('node')} 在途任务重路由到在线节点后排查该节点")
    for n in online:
        if 'engine' in n:
            # 引擎可插拔层：把"用了哪个引擎、是否已降级"纳入体检
            if n.get('degraded'):
                findings.append({
                    'severity': 'medium', 'code': 'engine_degraded',
                    'message': f"{n.get('node')} 引擎已降级"
                               f"（当前 {n.get('engine')}）"})
                recs.append(f"检查 {n.get('node')} 的引擎进程与显存占用，"
                            f"必要时用 engine-selector 重新选路")
            elif n.get('engine_healthy') is False:
                findings.append({
                    'severity': 'medium', 'code': 'engine_unhealthy',
                    'message': f"{n.get('node')} 引擎 {n.get('engine')} 探活失败"})
                recs.append(f"对 {n.get('node')} 执行引擎探活并排查端口/进程")
        if g(n, 'free_mem_gb', 99) < 1.0:
            findings.append({'severity': 'medium', 'code': 'low_free_mem',
                             'message': f"{n.get('node')} 空闲显存仅 "
                                        f"{n.get('free_mem_gb')}GB"})
            recs.append(f"对 {n.get('node')} 启用 gpu-slicer 切分或降低并发")
    for n in nodes:
        if n.get('cheat') or g(n, 'reputation', 1) < 0.9:
            findings.append({'severity': 'high', 'code': 'untrusted',
                             'message': f"{n.get('node')} 信誉="
                                        f"{n.get('reputation')} "
                                        f"cheat={n.get('cheat')}"})
            recs.append(f"隔离 {n.get('node')} 并对其结果做多数决复核")
    if online and avg_util < 10:
        findings.append({'severity': 'low', 'code': 'underutilized',
                         'message': f'平均利用率仅 {avg_util}%'})
        recs.append('使用 idle-onboarding 纳管闲置资源或合并实例')
    if not findings:
        findings.append({'severity': 'low', 'code': 'healthy',
                         'message': '全部节点在线且无明显异常'})
    return {
        'summary': {'nodes_total': len(nodes), 'online': len(online),
                    'lost': len(lost), 'avg_util': avg_util,
                    'low_mem_nodes': low_mem},
        'findings': findings,
        'recommendations': sorted(set(recs)),
        'nodes': nodes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ctrl', default='http://127.0.0.1:9000')
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    ap.add_argument('--state-file', default=None,
                    help='从 JSON 文件读取状态（离线/评测用），不访问控制面')
    a = ap.parse_args()
    try:
        if a.state_file:
            with open(a.state_file, encoding='utf-8') as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError('状态文件必须是 JSON 对象')
        else:
            state = fetch_state(a.ctrl, a.token)
            if not isinstance(state, dict):
                raise ValueError('控制面返回的不是 JSON 对象')
    except Exception as e:                     # noqa: BLE001
        # 结构化失败：不打印 traceback，交由调用方解析（原实现会抛栈）
        print(json.dumps({'error': f'{type(e).__name__}: {str(e)[:160]}',
                          'state_file': a.state_file, 'findings': []},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(diagnose(state), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
