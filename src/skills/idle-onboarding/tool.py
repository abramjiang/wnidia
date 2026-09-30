# -*- coding: utf-8 -*-
"""Idle Onboarding：探活并向控制面注册新节点，初始化画像/信誉/配额。

合规（手册 8.1-2：严禁扫描、探测内网中的其他节点）：
本工具会向 `--worker-host` 发起一次 /healthz 探测。**目标必须通过合规校验**：
默认只允许回环地址；确需纳管其他主机时，必须显式写入 WNIDIA_ALLOW_HOSTS 白名单。
校验失败直接拒绝执行并返回结构化错误（绝不"探测一下试试"）。
"""
import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
try:
    from controller import compliance as _comp
except Exception:                     # noqa: BLE001
    _comp = None

S = requests.Session(); S.trust_env = False


def check_target(host):
    """返回 (ok, 原因)。合规模块不可用时**保守拒绝**非回环目标。"""
    if _comp is not None:
        try:
            _comp.assert_probe_allowed(host, 'idle-onboarding 探活')
            return True, 'ok'
        except _comp.ComplianceError as e:
            return False, str(e)
    h = (host or '').strip().lower()
    if h in ('127.0.0.1', 'localhost', '::1'):
        return True, 'ok'
    return False, ('合规模块不可用，且目标非回环。手册 8.1-2 严禁探测内网其他节点，'
                   '已拒绝执行。')


def build_profile(a):
    return {
        'node': a.node, 'role': a.role, 'tier': a.tier,
        'mem_total_gb': a.mem_limit_gb, 'mem_limit_gb': a.mem_limit_gb,
        'compute_pct': a.compute_pct, 'vllm_port': a.vllm_port,
        'trusted': a.trusted, 'sovereign_region': a.sovereign_region,
        'gpu_name': a.gpu_name, 'engine': getattr(a, 'engine', 'mock'),
    }


def probe_health(a):
    url = f'http://{a.worker_host}:{a.vllm_port}/healthz'
    try:
        r = S.get(url, timeout=4)
        return {'reachable': r.status_code == 200, 'url': url}
    except requests.RequestException as e:
        return {'reachable': False, 'url': url, 'error': str(e)[:80]}


def onboard(a):
    profile = build_profile(a)
    ok, why = check_target(a.worker_host)
    if not ok:
        return {'onboarded': False, 'blocked_by_compliance': True,
                'node': a.node, 'profile': profile,
                'health': {'reachable': None, 'url': f'{a.worker_host}:{a.vllm_port}',
                           'skipped': '合规校验未通过，未发起探测'},
                'error': why,
                'next_steps': ['改用回环地址（如 127.0.0.1）本机纳管',
                               '确需纳管其他主机：把该主机写入 WNIDIA_ALLOW_HOSTS '
                               '白名单并确认其属于本队资产']}

    health = probe_health(a)

    if a.dry_run:
        return {'onboarded': False, 'dry_run': True, 'node': a.node,
                'profile': profile, 'health': health,
                'initial': {'reputation': 1.0,
                            'status': 'pending_first_heartbeat', 'quota': 1000},
                'next_steps': ['dry-run：确认无误后去掉 --dry-run 实际注册']}

    if not health['reachable']:
        return {'onboarded': False, 'node': a.node, 'profile': profile,
                'health': health,
                'next_steps': ['节点未探活：检查 worker 是否启动、端口与绑定地址'
                               '（worker 只绑 127.0.0.1 属预期，本机探测可达即可）',
                               '确认后重新执行纳管']}

    r = S.post(f'{a.ctrl}/internal/register', json=profile,
               headers={'Authorization': f'Bearer {a.token}'}, timeout=10)
    ok = r.status_code == 200
    return {
        'onboarded': ok, 'node': a.node, 'profile': profile, 'health': health,
        'initial': {'reputation': 1.0,
                    'status': 'pending_first_heartbeat', 'quota': 1000},
        'next_steps': ['等待节点首次心跳，状态转为 online',
                       '用 resource-doctor 复核，用 gpu-slicer 规划切分',
                       '用 engine-selector 确认该节点应绑定哪个推理引擎']
        if ok else [f'注册失败：{r.text[:80]}']
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--node', required=True)
    ap.add_argument('--tier', default='edge',
                    choices=['cloud', 'edge', 'home', 'cpu'])
    ap.add_argument('--role', default='decode',
                    choices=['prefill', 'decode', 'cpu'])
    ap.add_argument('--mem-limit-gb', type=float, default=8)
    ap.add_argument('--compute-pct', type=int, default=100)
    ap.add_argument('--vllm-port', type=int, default=8104)
    ap.add_argument('--worker-host', default='127.0.0.1')
    ap.add_argument('--gpu-name', default='GB10')
    ap.add_argument('--engine', default='mock',
                    choices=['mock', 'ollama', 'vllm', 'tensorfold'],
                    help='该节点实际绑定的推理引擎（引擎可插拔层）')
    ap.add_argument('--trusted', action='store_true')
    ap.add_argument('--sovereign-region', default='local')
    ap.add_argument('--ctrl', default=os.getenv('CTRL',
                                                'http://127.0.0.1:9000'))
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    print(json.dumps(onboard(a), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
