# -*- coding: utf-8 -*-
"""CC Auditor：机密计算层级与审计链体检（BP P9）。

回答三个问题：
  1. 某任务的数据密级（secret=L1–L4）要求什么机密计算层级（cc=CC-L0–CC-L4）？
  2. 目标节点当前能力是否达标、证明是否有效？
  3. 审计哈希链是否完整（有没有被改过）？

**边界**：不检查也不触碰硬件 TEE；证明来自控制面（沙盒为软件证明）。
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

SECRET_TO_MIN_CC = {'L1': 'CC-L0', 'L2': 'CC-L0', 'L3': 'CC-L2', 'L4': 'CC-L3'}
CC_RANK = {f'CC-L{i}': i for i in range(5)}


def satisfies(node_cc, required):
    return CC_RANK.get(node_cc, 0) >= CC_RANK.get(required, 0)


def audit_state(state, secret='L1', node=None, verify_chain=True):
    nodes = state.get('nodes') or []
    req = SECRET_TO_MIN_CC.get(secret, 'CC-L0')
    picked = [n for n in nodes if not node or n.get('node') == node]

    rows = []
    for n in picked:
        cc = n.get('cc_level') or 'CC-L0'
        ok = satisfies(cc, req)
        att = None
        for a in (state.get('attestations') or []):
            if a.get('node') == n.get('node'):
                att = a
                break
        att_ok = None
        if att:
            # 只采信快照里证明自身的 valid 标记 —— 本 Skill **不做**证明验签
            # （那需要真机 TEE 后端），避免给出"看起来验证过了"的错觉。
            att_ok = bool(att.get('valid'))
        rows.append({
            'node': n.get('node'), 'tier': n.get('tier'),
            'cc_level': cc, 'required': req, 'level_ok': ok,
            'attestation_present': att is not None,
            'attestation_valid': att_ok,
            'attestation_verifier': (att or {}).get('verifier'),
            'verdict': ('ok' if (ok and (att_ok or req == 'CC-L0'))
                        else 'blocked'),
        })

    chain = None
    if verify_chain:
        chain = _verify_chain(state.get('chain') or [])

    blocked = [r for r in rows if r['verdict'] == 'blocked']
    serviceable = [r['node'] for r in rows if r['verdict'] == 'ok']
    return {
        'secret': secret, 'required_cc': req,
        'nodes_checked': len(rows), 'nodes': rows,
        'blocked_nodes': [r['node'] for r in blocked],
        'serviceable_nodes': serviceable,
        # allow = 至少有一个节点满足要求（被拦的节点只是不入候选，不阻塞任务）
        'admission_result': ('allow' if serviceable
                             else ('blocked' if rows else 'no_nodes')),
        'chain': chain,
        'terminology': {
            'secret': '数据密级（任务侧）：L1 公开 / L2 内部 / L3 机密 / L4 绝密',
            'cc': '机密计算层级（节点侧）：CC-L0 无 / CC-L1 机密 GPU / '
                  'CC-L2 远程证明 / CC-L3 密钥与审计 / CC-L4 部署可选',
            'warning': '两套符号都含 L1–L4，语义完全不同；对外必须带前缀。',
        },
        'boundary': ('本 Skill 不检查硬件 TEE；沙盒环境下证明为软件签发，'
                     '真机需接 NVTrust / CC 机型。'),
    }


def _verify_chain(chain):
    if not chain:
        return {'length': 0, 'ok': None, 'note': '无审计链记录（快照未包含 chain）'}
    prev, broken = '', []
    for r in sorted(chain, key=lambda x: x.get('seq', 0)):
        raw = (f"{prev}|{r.get('kind')}|{r.get('ref')}|"
               f"{r.get('payload_digest')}|{r.get('ts')}")
        expect = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        if r.get('prev_hash') != prev:
            broken.append({'seq': r.get('seq'), 'why': 'prev_hash 断链'})
        elif r.get('chain_hash') != expect:
            broken.append({'seq': r.get('seq'), 'why': 'chain_hash 不匹配'})
        prev = r.get('chain_hash') or ''
    return {'length': len(chain), 'ok': not broken, 'broken': broken,
            'head': prev}


def fetch(ctrl, token, timeout=15):
    import requests
    s = requests.Session(); s.trust_env = False
    s.trust_env = False
    h = {'Authorization': f'Bearer {token}'}
    st = s.get(f'{ctrl.rstrip("/")}/admin/state', headers=h, timeout=timeout).json()
    try:
        st['chain'] = s.get(f'{ctrl.rstrip("/")}/admin/audit/verify',
                            headers=h, timeout=timeout).json().get('broken') or []
    except Exception:                                 # noqa: BLE001
        pass
    return st


def main():
    ap = argparse.ArgumentParser(description='机密计算层级与审计链体检')
    ap.add_argument('--secret', default='L1', choices=list(SECRET_TO_MIN_CC))
    ap.add_argument('--node', default=None)
    ap.add_argument('--state-file', default=None)
    ap.add_argument('--ctrl', default=os.getenv('CTRL', 'http://127.0.0.1:9000'))
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--no-chain', action='store_true', help='跳过哈希链校验')
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
        out = audit_state(state, secret=a.secret, node=a.node,
                          verify_chain=not a.no_chain)
        out['source'] = f'file:{a.state_file}'
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    try:
        state = fetch(a.ctrl, a.token)
    except Exception as e:                            # noqa: BLE001
        print(json.dumps({'error': f'控制面不可达：{str(e)[:120]}',
                          'hint': '可改用 --state-file 做离线体检'},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    out = audit_state(state, secret=a.secret, node=a.node,
                      verify_chain=not a.no_chain)
    out['source'] = f'ctrl:{a.ctrl}'
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
