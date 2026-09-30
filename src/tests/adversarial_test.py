# -*- coding: utf-8 -*-
"""第3轮 对抗/并发/边界测试。自带全新栈。退出码 0 表示全部通过。"""
import json
import os
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _clean import (preflight_ports, pgid_of, kill_tree,  # noqa: E402
                    robust_session, DEFAULT_PORTS)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = 'adv-token-2f9c41ba7e'
BASE = 'http://127.0.0.1:9000'
results = []
S = robust_session()   # 连接被服务端重置时自动重试（仅连接层）
H = {'Authorization': f'Bearer {TOKEN}'}


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def admin(path, method='POST', **kw):
    return S.request(method, f'{BASE}{path}', headers=H, timeout=30, **kw).json()


def state():
    r = S.get(f'{BASE}/admin/state', headers=H, timeout=20)
    if r.status_code != 200:
        # 旧实例残留会让新 Token 得到 401；不要让 KeyError('tasks') 掩盖真因
        raise SystemExit(
            f'[error] /admin/state 返回 {r.status_code}。'
            f'通常是端口被上一次未清理的实例占用。'
            f'请执行：lsof -ti:9000,8888 | xargs kill -9')
    return r.json()


def wait_tasks(pred, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred(state()):
            return True
        time.sleep(0.5)
    return False


def submit_spot():
    body = {'messages': [{'role': 'user',
            'content': '批量处理一批较长的素材需要运行一段时间 aaaa bbbb cccc dddd eeee'}],
            'task_type': 'batch', 'sla': 'sla-3'}
    try:
        S.post(f'{BASE}/v1/chat/completions', json=body,
               headers=H, timeout=1.5)
    except requests.exceptions.ReadTimeout:
        pass


def newest_spot(st):
    runs = [t for t in st['tasks'] if t['sla'] == 'sla-3'
            and t['state'] in ('running', 'binding')]
    runs.sort(key=lambda t: t['created_at'])
    return runs[-1]['task'] if runs else None


def main():
    preflight_ports(DEFAULT_PORTS)
    env = dict(os.environ, WNIDIA_TOKEN=TOKEN, RESET_DB='1',
               WNIDIA_JEV_MODE='mock')
    env.setdefault('WNIDIA_PY', sys.executable)
    proc = subprocess.Popen(['bash', 'scripts/run_local.sh'], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pgid = pgid_of(proc)
    try:
        for _ in range(30):
            try:
                S.get(f'{BASE}/healthz', timeout=2); break
            except requests.RequestException:
                time.sleep(1)
        time.sleep(9)

        # ---- A: 抢占/恢复竞态反复压（随机化注入时机）----
        import random
        stuck = 0
        for cyc in range(8):
            submit_spot()
            time.sleep(random.uniform(0.6, 3.0))
            st = state()
            spot = newest_spot(st)
            if spot is None:
                continue
            r = S.post(f'{BASE}/admin/inject/preempt', headers=H, timeout=20).json()
            if not r.get('ok'):
                continue
            ok = wait_tasks(
                lambda d: any(t['task'] == spot and t['state'] == 'done'
                              for t in d['tasks']) and
                          any(t['sla'] == 'sla-1' and t['state'] == 'done'
                              for t in d['tasks']), timeout=28)
            if not ok:
                stuck += 1
        check('抢占/恢复 8 轮无挂死', stuck == 0, f'挂死轮数={stuck}')

        # ---- B: 不足 3 节点时多数决不惩罚（确定性：reset 后只注册 2 节点）----
        S.post(f'{BASE}/admin/reset', headers=H, timeout=15)
        def register_node(node, role, tier, port, mem, trusted=False):
            S.post(f'{BASE}/internal/register', headers=H, timeout=15, json={
                'node': node, 'role': role, 'tier': tier,
                'mem_total_gb': mem, 'mem_limit_gb': mem,
                'compute_pct': 100, 'vllm_port': port, 'trusted': trusted,
                'sovereign_region': 'local'})
        register_node('cloud-0', 'prefill', 'cloud', 8101, 40, trusted=True)
        register_node('edge-1', 'decode', 'edge', 8102, 8)
        time.sleep(6)   # 两节点心跳稳定；cpu-1 心跳因 404 不会被加回
        rep_before = {n['node']: n['reputation'] for n in state()['nodes']}
        S.post(f'{BASE}/v1/chat/completions', headers=H,
               json={'messages': [{'role': 'user', 'content': '请校验关键结论'}],
                     'verify': True}, timeout=30)
        st = state()
        guard = any('节点不足' in e['message'] for e in st['events'])
        rep_after = {n['node']: n['reputation'] for n in st['nodes']}
        check('2 节点多数决守卫', guard and rep_before == rep_after,
              f'guard={guard}')
        # 恢复完整 3 节点，供后续用例
        register_node('cpu-1', 'cpu', 'cpu', 8103, 4)
        time.sleep(3)

        # ---- C: 边界输入不崩溃 ----
        r1 = S.post(f'{BASE}/v1/chat/completions', headers=H,
                    json={'messages': []}, timeout=20)
        check('空 messages 不 500', r1.status_code in (200, 422),
              f'http={r1.status_code}')
        # v5：空输入必须按**输入错误**在入口拒掉，不能异步变成 409，
        #     更不能在任务列表里留下一条 prompt_empty 的 FAILED 垃圾记录。
        check(' C0 空输入在入口被拒（不当成任务、不留垃圾记录）',
              r1.status_code == 422
              and not any(t.get('error') == 'prompt_empty'
                          for t in state()['tasks']),
              f"http={r1.status_code}")
        longp = '长文本 ' * 900   # >1500 tokens -> heavy/prefill
        r2 = S.post(f'{BASE}/v1/chat/completions', headers=H,
                    json={'messages': [{'role': 'user', 'content': longp}]},
                    timeout=40)
        check('超长 prompt(heavy) 完成', r2.status_code == 200,
              f'http={r2.status_code}')
        r3 = S.post(f'{BASE}/v1/chat/completions', headers=H,
                    json={'messages': [{'role': 'user', 'content': 'hi'}],
                          'sla': 'weird-sla'}, timeout=30)
        check('异常 sla 不崩溃', r3.status_code == 200, f'http={r3.status_code}')

        # ---- D: 无 spot 时重复抢占优雅返回 ----
        a = S.post(f'{BASE}/admin/inject/preempt', headers=H, timeout=15).json()
        b = S.post(f'{BASE}/admin/inject/preempt', headers=H, timeout=15).json()
        check('重复抢占优雅处理', (not a.get('ok')) or (not b.get('ok')))

        # ---- E: 鉴权 ----
        no_tok = S.post(f'{BASE}/admin/reset', timeout=10).status_code
        bad_tok = S.post(f'{BASE}/admin/reset',
                         headers={'Authorization': 'Bearer wrong'},
                         timeout=10).status_code
        check('鉴权拒绝无/错 Token', no_tok == 401 and bad_tok == 401,
              f'{no_tok}/{bad_tok}')

        # ---- F: 隔离期语义 —— 掉线后先"粘住"，隔离期结束再被心跳带回 ----
        # 本轮改动：/admin/node/lost 会写入 quarantine，心跳在隔离期内只更新
        # 指标、不改变在线状态，避免"掉线 5 秒自己复活"让重调度无从观察。
        S.post(f'{BASE}/admin/node/lost?node=cpu-1', headers=H, timeout=15)
        time.sleep(4)
        st = state()
        sticky = [n for n in st['nodes'] if n['node'] == 'cpu-1'][0]['status'] \
            == 'lost'
        check('隔离期内保持离线（粘住）', sticky)
        hb = int(os.getenv('WNIDIA_HB_TIMEOUT', '15'))
        recovered = wait_tasks(
            lambda d: any(n['node'] == 'cpu-1' and n['status'] == 'online'
                          for n in d['nodes']), timeout=hb + 12)
        check('隔离期结束后由心跳恢复 online', recovered)

        # ---- G: 重复注册改 mem_limit 生效 ----
        payload = {'node': 'edge-1', 'role': 'decode', 'tier': 'edge',
                   'mem_total_gb': 12, 'mem_limit_gb': 12, 'compute_pct': 60,
                   'vllm_port': 8102, 'sovereign_region': 'local'}
        S.post(f'{BASE}/internal/register', json=payload, headers=H, timeout=15)
        time.sleep(1)
        n = [x for x in state()['nodes'] if x['node'] == 'edge-1'][0]
        check('重复注册覆盖显存', n['mem_limit_gb'] == 12,
              f"mem_limit={n['mem_limit_gb']}")

        # ---- H: 合规守卫（手册 8.1-2 / 8.2-9）----
        comp = admin('/admin/compliance', 'GET')
        check('合规视图：仅 8888/9000 属公网端口',
              comp.get('public_ports') == [8888, 9000],
              f"ports={comp.get('public_ports')}")
        check('合规视图：凭据输出已脱敏',
              '*' in (comp.get('token_masked') or '')
              and TOKEN not in json.dumps(comp),
              f"masked={comp.get('token_masked')}")

        # ---- I: 引擎可插拔层 ----
        eng = admin('/admin/engines?probe=false', 'GET')
        dec = eng.get('decision', {})
        check('引擎选路返回降级链与一致性说明',
              bool(dec.get('fallback_chain')) and
              bool(dec.get('consistency_guarantee')),
              f"selected={dec.get('selected')}")
        bad = S.get(f'{BASE}/admin/engines', headers=H,
                    params={'timeout': 'not-a-number'}, timeout=15)
        check('引擎探活非法参数不 500', bad.status_code in (200, 422),
              f'http={bad.status_code}')

        # ================= v4 新增能力的边界与对抗 =================

        # ---- J: 批量入队注入的非法入参 ----
        j1 = S.post(f'{BASE}/admin/inject/queue', headers=H,
                    json={'tasks': []}, timeout=15)
        check(' J1 空任务列表不 500', j1.status_code in (200, 422),
              f'http={j1.status_code}')
        j2 = S.post(f'{BASE}/admin/inject/queue', headers=H,
                    json={'tasks': [{'sla': 'weird', 'need_mem_gb': 'abc'}]},
                    timeout=15)
        check(' J2 非法 sla / 非法数值不 500', j2.status_code in (200, 422),
              f'http={j2.status_code}')
        j3 = S.post(f'{BASE}/admin/inject/queue', headers=H, json={}, timeout=15)
        check(' J3 缺 tasks 字段不 500', j3.status_code in (200, 422),
              f'http={j3.status_code}')

        # ---- K: 断网批次回放：幂等 + 非法请求 ----
        batch = {'batch_id': 'adv-batch-1', 'node': 'edge-1', 'tasks': 2,
                 'tokens': 500, 'node_seconds': 12.5, 'note': '对抗测试'}
        k1 = admin('/internal/offline/batch', 'POST', json=batch)
        k2 = admin('/internal/offline/batch', 'POST', json=batch)
        check(' K4 同批次重复回放只计一次（幂等）',
              bool(k1) and (k1.get('duplicate') or k2.get('duplicate')),
              f"first_dup={bool(k1.get('duplicate'))} "
              f"second_dup={bool(k2.get('duplicate'))}")
        k3 = S.post(f'{BASE}/internal/offline/batch', headers=H,
                    json={'node': 'edge-1'}, timeout=15)
        check(' K5 缺 batch_id 被 422 拒绝', k3.status_code in (422, 400),
              f'http={k3.status_code}')
        off = admin('/admin/offline', 'GET')
        check(' K6 回放批次被计入控制面', off.get('replayed', 0) >= 1,
              f"replayed={off.get('replayed')} "
              f"tokens={off.get('tokens_compensated')}")

        # ---- L: 门槛度量：样本不足不得判 GO ----
        g = admin('/admin/gates', 'GET')
        gates = g.get('gates', [])
        ok_l = len(gates) == 5 and all(
            x.get('verdict') != 'go' or (x.get('sample') or 0) >=
            (x.get('min_sample') or 0) for x in gates)
        check(' L7 五条门槛齐全且样本不足不判 GO', ok_l,
              f"overall={g.get('overall')} "
              f"verdicts={[x.get('verdict') for x in gates]}")
        check(' L8 每条门槛都报出样本量',
              all('sample' in x for x in gates),
              f"samples={[x.get('sample') for x in gates]}")

        # ---- M: 结算：重复执行不重复计账 ----
        s1 = admin('/admin/settlement', 'POST', params={'persist': 'true'})
        n1 = admin('/admin/settlement', 'GET').get('records', 0)
        s2 = admin('/admin/settlement', 'POST', params={'persist': 'true'})
        n2 = admin('/admin/settlement', 'GET').get('records', 0)
        check(' M9 重复结算不产生重复记录（幂等）',
              bool(s1) and bool(s2) and n1 == n2,
              f'records {n1} -> {n2}')
        check(' M10 分成比例显式标注为规划假设',
              s1.get('assumption') is True and bool(s1.get('note')),
              f"ratio={s1.get('owner_share_ratio')}")

        # ---- N: QPU 抽象：非法电路/后端/量子位数 ----
        bell = [{'gate': 'h', 'q': 0}, {'gate': 'cx', 'c': 0, 't': 1},
                {'gate': 'measure', 'q': 0}, {'gate': 'measure', 'q': 1}]
        n1_ = admin('/admin/qpu/submit', 'POST',
                    json={'circuit': bell, 'qubits': 99})
        check(' N11 超上限量子位被拒', n1_.get('ok') is False,
              str(n1_.get('reason'))[:50])
        n2_ = admin('/admin/qpu/submit', 'POST',
                    json={'circuit': bell, 'qubits': 2, 'backend': 'cudaq'})
        check(' N12 CUDA-Q 未安装时拒绝而非静默回退',
              n2_.get('ok') is False and 'CUDA-Q' in str(n2_.get('reason')),
              str(n2_.get('reason'))[:50])
        n3_ = admin('/admin/qpu/submit', 'POST',
                    json={'circuit': [{'gate': 'not-a-gate', 'q': 0}],
                          'qubits': 2})
        check(' N13 未知门被拒', n3_.get('ok') is False,
              str(n3_.get('reason'))[:50])

        # ---- O: 机队：未注册机型/非法比例 ----
        o1 = admin('/admin/fleet/rollout', 'POST',
                   json={'model_name': 'no-such-model', 'version': 'v9',
                         'plan_stages': True})
        check(' O14 未注册机型灰度不崩', isinstance(o1, dict),
              f"keys={sorted(o1)[:4]}")
        o2 = admin('/admin/fleet/rollout', 'POST',
                   json={'model_name': 'edge-brain', 'version': 'v2',
                         'rollout_pct': 250})
        check(' O15 非法灰度比例被拒', o2.get('ok') is False,
              str(o2.get('reason'))[:40])
        o3 = admin('/admin/fleet/uplink', 'POST',
                   json={'robot': 'r-ghost', 'payload': 'a@b.com <LAN_GW>'})
        check(' O16 未注册机器人回传不 500', isinstance(o3, dict),
              f"keys={sorted(o3)[:4]}")

        # ---- P: 分时复用非法窗口 ----
        p1 = S.get(f'{BASE}/admin/time-slice', headers=H,
                   params={'window_minutes': 10, 'slice_minutes': 60},
                   timeout=15).json()
        check(' P17 时片大于窗口被拒', p1.get('ok') is False,
              str(p1.get('reason'))[:40])
        p2 = S.get(f'{BASE}/admin/time-slice', headers=H,
                   params={'window_minutes': 0, 'slice_minutes': 15},
                   timeout=15).json()
        check(' P18 零窗口被拒', p2.get('ok') is False,
              str(p2.get('reason'))[:40])

        # ---- Q: 非法密级 / 机密计算层级不打断闭环 ----
        q1 = S.post(f'{BASE}/v1/chat/completions', headers=H,
                    json={'messages': [{'role': 'user', 'content': 'hi'}],
                          'secret': 'L9'}, timeout=30)
        check(' Q19 非法密级不崩（按最低密级容错）',
              q1.status_code in (200, 409), f'http={q1.status_code}')
        q2 = S.post(f'{BASE}/v1/chat/completions', headers=H,
                    json={'messages': [{'role': 'user', 'content': 'hi'}],
                          'cc_required': 'CC-L9'}, timeout=30)
        check(' Q20 非法机密计算层级不崩', q2.status_code in (200, 409, 422),
              f'http={q2.status_code}')

        # ---- R: 审计证据包不可篡改 ----
        pack = admin('/admin/audit/export', 'POST', params={'since_ms': 0})
        sys.path.insert(0, ROOT)
        from controller import trust as _trust
        v_ok = _trust.verify_evidence_pack(pack)
        tampered = json.loads(json.dumps(pack))
        tampered.setdefault('counts', {})['tasks'] = 999999
        v_bad = _trust.verify_evidence_pack(tampered)
        check(' R21 证据包签名可校验且篡改可检出',
              v_ok.get('ok') and v_bad.get('ok') is False,
              f"ok={v_ok.get('ok')} tampered_ok={v_bad.get('ok')}")
        check(' R22 证据包默认不含 prompt 明文',
              pack.get('prompts_included') is False,
              f"prompts_included={pack.get('prompts_included')}")

        # ---- S: 隐私策略不能靠一个查询参数绕过（v5）----
        rv = S.get(f'{BASE}/admin/state', headers=H,
                   params={'reveal_prompt': 'true'}, timeout=20)
        rj = rv.json()
        states = {t.get('prompt_state') for t in rj.get('tasks', [])}
        plain = [t for t in rj.get('tasks', [])
                 if t.get('prompt_state') == 'available']
        check(' S1 单靠 reveal_prompt=1 拿不到明文（两个开关是「与」）',
              rv.status_code == 200 and not plain
              and states <= {'redacted', 'released'},
              f'http={rv.status_code} states={sorted(s for s in states if s)}')
        check(' S2 脱敏输出仍带摘要与长度（脱敏 ≠ 查不到）',
              all(('prompt_digest' in t and 'prompt_chars' in t)
                  for t in rj.get('tasks', [])),
              f"tasks={len(rj.get('tasks', []))}")
        check(' S3 对外 JSON 里不出现占位标记原文（连痕迹也不给）',
              '[REDACTED:prompt-not-stored]' not in rv.text,
              '占位标记只在库里，不应出现在对外响应')
    finally:
        kill_tree(pgid, proc)

    failed = [r for r in results if not r[1]]
    print('\n' + '=' * 40)
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
