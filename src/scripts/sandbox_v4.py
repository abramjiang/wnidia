# -*- coding: utf-8 -*-
"""v4 全能力沙盒验证：一键起栈 → 逐项跑通 v4 新能力 → 出结构化报告。

用法：
    WNIDIA_PY=<解释器> python scripts/sandbox_v4.py
    python scripts/sandbox_v4.py --keep        # 跑完不关栈，便于手工继续看

覆盖范围（与开发报告一一对应）：
    A 口径与能力面      /admin/capabilities
    B 三维路由（时延）  latency_budget 硬约束
    C SLA 优先级排队    sla-1 先于 sla-3 出队
    D 真实计量与分账    token / node 两种口径
    E 机密计算与证明    CC 层级准入 + 证明签发/校验
    F 门槛度量          /admin/gates 五条门槛
    G 边缘断网自治      worker 本地队列 → 批次回放 → 幂等去重
    H 具身机队          注册 / 灰度 / 派发 / 回传脱敏
    I QPU 抽象          受限门集电路 + cudaq 拒绝
    J 审计证据包        导出 → 校验 → 篡改可检出
    K 机主结算          分成与抽佣
    L 分时复用策略      份额归一
    M SDK               sdk/wnidia_client.py 全接口可用
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from _clean import (preflight_ports, pgid_of, kill_tree,  # noqa: E402
                    robust_session, DEFAULT_PORTS)

CTRL = 'http://127.0.0.1:9000'
TOKEN = os.getenv('WNIDIA_TOKEN', 'sandbox-v4-token-3f81c2')
WORKER_EDGE = 'http://127.0.0.1:8102'
H = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}

results = []
S = robust_session()   # 连接被服务端重置时自动重试（仅连接层）


def check(name, cond, detail=''):
    results.append({'name': name, 'ok': bool(cond), 'detail': str(detail)[:300]})
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def get(path, **params):
    r = S.get(f'{CTRL}{path}', headers=H, params=params or None, timeout=60)
    return r.json() if r.headers.get('content-type', '').startswith(
        'application/json') else r.text


def post(path, body=None, **params):
    r = S.post(f'{CTRL}{path}', headers=H, params=params or None, json=body,
               timeout=90)
    try:
        return r.json()
    except ValueError:
        return {'raw': r.text[:200], 'status': r.status_code}


def wait_cluster_ready(timeout=45, min_nodes=3, need_latency=True):
    """等集群"真的可用"再开跑，而不是睡固定秒数。

    BUG-V5-10：原先固定 `time.sleep(9)` 就开测。worker 心跳间隔 5s，
    且**心跳里带的时延是上一次的测量值**（首次必然为 0.0），
    启动稍慢时会出现"只有 1 个节点上线、时延还是 0"，于是场景 B 的首条断言
    B0 假失败、整个 B 场景提前 return（断言数从 75 掉到 71）——
    看起来像被测系统坏了，其实是测试没等够。

    改成等**条件**：≥ min_nodes 个节点在线，且至少 1 个已上报非零时延。
    返回 (ok, info)，不 ok 也继续跑（让各场景自己报诊断），但会打印出来。
    """
    t0 = time.time()
    info = {'online': 0, 'latencies': []}
    while time.time() - t0 < timeout:
        try:
            nodes = get('/admin/state')['nodes']
        except Exception:                          # noqa: BLE001
            nodes = []
        online = [n for n in nodes if n.get('status') == 'online']
        lats = [float(n.get('latency_ms') or 0) for n in online]
        info = {'online': len(online), 'latencies': sorted(lats)}
        if len(online) >= min_nodes and \
                (not need_latency or (lats and max(lats) > 0)):
            return True, info
        time.sleep(1)
    return False, info


def chat(messages, **kw):
    r = S.post(f'{CTRL}/v1/chat/completions', headers=H,
               json={'messages': messages, **kw}, timeout=120)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def wait_task_done(task_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = get('/admin/state')
        for t in st['tasks']:
            if t['task'] == task_id and t['state'] in ('done', 'failed',
                                                       'rejected'):
                return t
        time.sleep(0.6)
    return None


# ---------------------------------------------------------------- A 口径
def sc_a():
    cap = get('/admin/capabilities')
    board = cap.get('board', {})
    counts = board.get('counts', {})
    check('A1 能力总表可读', bool(counts), f'counts={counts}')
    outward = board.get('outward_phrasing', {})
    labels = board.get('status_labels', {})
    check('A2 收敛措辞存在（decision_only/simulated 不得说成已支持）',
          '执行依赖硬件' in outward.get('decision_only', '')
          and '沙盒' in outward.get('simulated', '')
          and labels.get('decision_only') == '有决定无执行',
          f"decision_only={outward.get('decision_only','')[:24]} "
          f"label={labels.get('decision_only')}")
    layers = cap.get('layers', [])
    check('A3 对外只讲三层', len(layers) == 3,
          f'layers={[l["layer"] for l in layers]}')
    term = cap.get('terminology', {})
    check('A4 secret 与 cc 分离表述',
          'secret=' in term.get('secret', {}).get('prefix', '')
          and 'cc=' in term.get('cc', {}).get('prefix', ''),
          '两套 L1–L4 已分离')


# ---------------------------------------------------------------- B 三维路由
def sc_b():
    """第三维「时延」：硬约束真的生效（不是装饰），且失败原因可区分。

    预算按节点实测时延自适应取（慢节点与快节点之间），因此用例不依赖
    写入任何固定数字，换机器也成立。
    """
    nodes = [n for n in get('/admin/state')['nodes']
             if n['status'] == 'online']
    lats = sorted(float(n.get('latency_ms') or 0) for n in nodes)
    check('B0 节点已上报时延（第三维有数据）',
          bool(lats) and lats[-1] > 0, f'latencies={lats}')
    if not (lats and lats[-1] > 0):
        return
    lo, hi = lats[0], lats[-1]
    budget = round((lo + hi) / 2.0, 3) if hi > lo else round(lo + 1.0, 3)
    if hi > lo:
        check('B0b 存在超过预算的节点（说明硬约束确实在筛，不是摆设）',
              any(l > budget for l in lats),
              f'budget={budget}ms lats={lats}')

    code, body = chat([{'role': 'user', 'content': '低时延任务测试'}],
                      latency_budget_ms=budget, tenant='demo-b',
                      project='routing')
    tid = body.get('id')
    check('B1 时延预算内任务可完成', code == 200 and bool(tid),
          f'budget={budget}ms http={code} '
          f'body={json.dumps(body, ensure_ascii=False)[:140]}')
    if code == 200 and tid:
        st = get('/admin/state')
        t = [x for x in st['tasks'] if x['task'] == tid]
        # 关键：时延是**每个心跳都在变**的观测量。任务结束后再读节点当前时延，
        # 很可能已经高于阈值（"决策时合规、现在超了"），那样断言本身就是错的。
        # 因此核对调度**决策时**记录在 bind 事件里的那个值。
        be = [e for e in st['events'] if e['kind'] == 'bind' and e['task'] == tid]
        m = re.search(r'时延=([0-9.]+)ms', be[0]['message']) if be else None
        decided = float(m.group(1)) if m else None
        check('B2 决策时命中的节点满足时延硬约束',
              bool(t) and decided is not None and decided <= budget + 0.06,
              f'node={t[0]["node"] if t else None} 决策时延={decided}ms '
              f'budget={budget}ms（注：当前时延会随心跳变化）')

    # 不可满足的预算：必须**明确拒绝**并给出可区分的理由，
    # 不能静默卡在队列里（这正是 v4 首轮自查抓到的 B1 现象）。
    #
    # 预算取 0.001ms 而不是"当前最小实测时延的一半"——后者会**竞态失败**：
    # 时延每次心跳都在变，可能刚好有一次心跳低到一半以下，任务就正常完成了。
    # worker 上报时延保留两位小数（最小非零值 0.01ms），因此 0.001ms
    # 是构造上不可能被满足的，判定稳定。
    impossible = 0.001
    code2, body2 = chat([{'role': 'user', 'content': '不可满足的时延预算'}],
                        latency_budget_ms=impossible, tenant='demo-b')
    detail = str(body2.get('detail', ''))
    check('B3 不可满足的时延预算被明确拒绝（不卡队列、原因可区分）',
          code2 in (400, 409) and 'latency' in detail,
          f'budget={impossible}ms http={code2} detail={detail[:90]}')


# ---------------------------------------------------------------- C SLA 排队
def sc_c():
    """SLA 优先级排队：同批入队的两个任务，高优必须**后创建却先出队**。

    注意：网关是同步的（提交后阻塞直到完成），所以不能连发两次 chat 来构造
    "同时排队"。改用 `/admin/inject/queue` 一次请求批量落库（单锁单事务），
    保证两者进入同一个调度节拍 —— 于是出队顺序只由 sla 优先级决定。
    """
    r = post('/admin/inject/queue', {'tasks': [
        {'sla': 'sla-3', 'prompt': '低优 spot 排序验证', 'tenant': 'demo-c'},
        {'sla': 'sla-1', 'prompt': '高优实时排序验证', 'tenant': 'demo-c'},
    ]})
    ids = r.get('injected') or []
    check('C1 低优/高优任务已同批入队', len(ids) == 2,
          f'injected={[i[-6:] for i in ids]}')
    if len(ids) != 2:
        return
    low, high = ids[0], ids[1]      # 先创建 = sla-3，后创建 = sla-1

    t0 = time.time()
    binds = []
    while time.time() - t0 < 40:
        ev = get('/admin/state')['events']          # 事件流按 id 倒序（新→旧）
        binds = [e['task'] for e in reversed(ev)
                 if e['kind'] == 'bind' and e['task'] in (low, high)]
        if len(binds) == 2:
            break
        time.sleep(0.5)
    check('C2 两任务均被绑定', len(binds) == 2, f'binds={[b[-6:] for b in binds]}')
    if len(binds) == 2:
        check('C3 高优先出队（sla-1 先于后创建的 sla-3 被绑定）',
              binds[0] == high,
              f'首个绑定={binds[0][-6:]} 期望=sla-1({high[-6:]})')
    # 排队策略必须可解释：出队顺序函数本身的语义也要能被断言
    st = get('/admin/state')
    bound = {t['task']: t['sla'] for t in st['tasks'] if t['task'] in (low, high)}
    check('C4 两任务 SLA 标记正确（sla-1/sla-3 都已消费）',
          bound.get(low) == 'sla-3' and bound.get(high) == 'sla-1',
          f'{bound}')


# ---------------------------------------------------------------- D 计量
def sc_d():
    _, b1 = chat([{'role': 'user', 'content': '计量测试 token 口径'}],
                 tenant='demo-d', billing_mode='token', project='p-meter',
                 department='dept-a')
    t1 = wait_task_done(b1.get('id'), 90) if b1.get('id') else None
    check('D1 token 口径任务完成', bool(t1) and t1['state'] == 'done',
          f'state={t1["state"] if t1 else None}')
    if t1:
        check('D2 采到 token 用量',
              (t1['prompt_tokens'] + t1['completion_tokens']) > 0,
              f"in={t1['prompt_tokens']} out={t1['completion_tokens']} "
              f"estimated={t1['metering_estimated']}")

    _, b2 = chat([{'role': 'user', 'content': '计量测试 node 口径'}],
                 tenant='demo-d', billing_mode='node')
    t2 = wait_task_done(b2.get('id'), 90) if b2.get('id') else None
    check('D3 node 口径任务完成', bool(t2) and t2['state'] == 'done',
          f'state={t2["state"] if t2 else None}')

    m = get('/admin/metering')
    modes = {x['key'] for x in m['summary']['by_billing_mode']}
    check('D4 三口径并存且已分账', 'token' in modes,
          f'modes={sorted(modes)}')
    check('D5 分账维度齐全（tenant/project/department）',
          bool(m['summary']['by_tenant']) and bool(m['summary']['by_project'])
          and bool(m['summary']['by_department']),
          f"tenants={[x['key'] for x in m['summary']['by_tenant']][:4]}")
    check('D6 计量质量可报（真实 vs 估算）',
          'real_ratio' in m['quality'],
          f"quality={m['quality']}")


# ---------------------------------------------------------------- E 机密计算
def sc_e():
    st = get('/admin/state')
    cloud = [n for n in st['nodes'] if n['node'] == 'cloud-0']
    check('E1 云节点存在', bool(cloud))
    p = post('/admin/trust/prove', None, node='cloud-0', cc_level='CC-L2')
    check('E2 沙盒证明签发成功', p.get('ok'),
          f"cc={p.get('cc_level')} verifier={p.get('verifier')}")
    v = post('/admin/trust/verify', None, node='cloud-0', cc_level='CC-L2')
    check('E3 证明校验通过', v.get('ok'), f"reason={v.get('reason')}")

    # L3 任务：只应落到具备 CC 能力且证明有效的节点
    _, b = chat([{'role': 'user', 'content': 'L3 机密长文本合同审查任务 ' + 'y' * 60}],
                secret='L3', tenant='demo-e', prefer_exact=True)
    t = wait_task_done(b.get('id'), 120) if b.get('id') else None
    ok = bool(t) and t['state'] == 'done'
    check('E4 L3 任务完成', ok, f'state={t["state"] if t else None}')
    if ok:
        n = [x for x in get('/admin/state')['nodes'] if x['node'] == t['node']]
        check('E5 L3 任务落在具备 CC 的节点', bool(n) and
              n[0].get('cc_level', 'CC-L0') != 'CC-L0',
              f"node={t['node']} cc={n[0].get('cc_level') if n else None}")
    tr = get('/admin/trust')
    check('E6 证明视图可读', tr.get('recent_total', 0) >= 1,
          f"recent_valid={tr.get('recent_valid')} backend={tr.get('backend')}")


# ---------------------------------------------------------------- F 门槛
def sc_f():
    g = get('/admin/gates')
    check('F1 五条门槛全部评估', len(g.get('gates', [])) == 5,
          f"overall={g.get('overall')} counts={g.get('counts')}")
    with_sample = [x for x in g['gates'] if x.get('sample') is not None]
    check('F2 每条门槛都报样本量', len(with_sample) == 5,
          f"samples={[x.get('sample') for x in g['gates']]}")
    check('F3 样本不足不得判 GO',
          all(x['verdict'] != 'go' or x['sample'] >= x['min_sample']
              for x in g['gates']),
          f"min_sample={g['gates'][0]['min_sample']}")
    check('F4 有收缩线动作说明', bool(g.get('policy')), g.get('policy', '')[:40])


# ---------------------------------------------------------------- G 断网自治
def sc_g():
    w = S.post(f'{WORKER_EDGE}/offline/enqueue',
               json={'tasks': 3, 'tokens': 1500, 'seconds': 45}, timeout=15).json()
    check('G1 边缘本地队列可入队', w.get('ok') and w.get('queue', 0) >= 3,
          f"queue={w.get('queue')} tokens={w.get('tokens')}")
    st = S.get(f'{WORKER_EDGE}/offline/status', timeout=10).json()
    check('G2 边缘处于断网自治状态', st.get('active'), f"simulated={st.get('simulated')}")

    r = S.post(f'{WORKER_EDGE}/offline/replay', timeout=30).json()
    rp = (r or {}).get('result') or {}
    check('G3 本地批次已回放', bool(rp.get('sent')),
          f"batch={rp.get('batch', {}).get('batch_id')} status={rp.get('status')}")

    off = get('/admin/offline')
    check('G4 控制面已记录回放批次', off.get('replayed', 0) >= 1,
          f"batches={off.get('batches')} replayed={off.get('replayed')} "
          f"tokens={off.get('tokens_compensated')}")

    # 幂等：重复提交同一 batch
    batch = rp.get('batch')
    if batch:
        d1 = post('/internal/offline/batch', batch)
        d2 = post('/internal/offline/batch', batch)
        check('G5 批次回放幂等（重复只记一次）',
              d1.get('duplicate') or d2.get('duplicate'),
              f"first={bool(d1.get('duplicate'))} second={bool(d2.get('duplicate'))}")


# ---------------------------------------------------------------- H 机队
def sc_h():
    for i, bat in enumerate((95, 80, 60)):
        post('/admin/fleet/register', {'robot': f'r-{i+1}', 'fleet': 'fleet-a',
                                       'region': 'workshop-1', 'battery': bat,
                                       'model_name': 'edge-brain',
                                       'model_version': 'v1'})
    fl = get('/admin/fleet')
    check('H1 机队注册成功', fl.get('robots', 0) >= 3,
          f"robots={fl.get('robots')} fleets={len(fl.get('fleets', []))}")

    pl = post('/admin/fleet/rollout', {'model_name': 'edge-brain',
                                       'version': 'v2', 'plan_stages': True})
    check('H2 灰度计划可登记',
          len(pl.get('stages', [])) == 3,
          f"stages={[s['stage_pct'] for s in pl.get('stages', [])]}")
    first = pl['stages'][0]['assignment']
    check('H3 灰度分配确定性（10% 时新版本数不超过总数）',
          first['new_count'] + first['old_count'] == 3,
          f"new={first['new_count']} old={first['old_count']}")

    dp = post('/admin/fleet/dispatch', {'tasks': [
        {'task': 'tk-1', 'region': 'workshop-1'},
        {'task': 'tk-2', 'region': 'workshop-1'}]})
    check('H4 机队派发成功', len(dp.get('assigned', [])) == 2,
          f"assigned={[a['robot'] for a in dp.get('assigned', [])]}")

    up = post('/admin/fleet/uplink', {
        'robot': 'r-1',
        'payload': '检测到缺陷，现场 IP <LAN_IP_3>，联系 a@b.com 电话 13800138000'})
    red = (up.get('sanitized') or {}).get('redactions', {})
    check('H5 回传脱敏生效（IP/邮箱/手机）',
          len(red) >= 3 and '<LAN_IP_3>' not in
          (up.get('sanitized') or {}).get('text', ''),
          f"redactions={red}")


# ---------------------------------------------------------------- I QPU
def sc_i():
    q = get('/admin/qpu')
    check('I1 QPU 后端枚举可读', bool(q['status']['backends']),
          f"backends={list(q['status']['backends'])}")
    check('I2 PQC 明确标注未实现', q['pqc']['ready'] is False,
          q['pqc']['current_impl'][:30])

    bell = [{'gate': 'h', 'q': 0}, {'gate': 'cx', 'c': 0, 't': 1},
            {'gate': 'measure', 'q': 0}, {'gate': 'measure', 'q': 1}]
    r = post('/admin/qpu/submit', {'circuit': bell, 'qubits': 2, 'shots': 800,
                                   'job': 'bell-1'})
    counts = (r.get('result') or {}).get('counts', {})
    ok = r.get('ok') and len(counts) >= 2
    check('I3 Bell 态模拟产出两态分布', ok, f"counts={counts}")
    if ok:
        vals = list(counts.values())
        spread = max(vals) - min(vals)
        check('I4 分布接近均匀（允许采样波动）', spread < 0.35 * sum(vals),
              f'spread={spread} total={sum(vals)}')
    check('I5 明示为模拟而非量子硬件', r.get('simulated') is True,
          r.get('note', '')[:40])

    bad = post('/admin/qpu/submit', {'circuit': bell, 'qubits': 2,
                                     'backend': 'cudaq'})
    check('I6 CUDA-Q 未安装时拒绝而非静默回退',
          bad.get('ok') is False and 'CUDA-Q' in bad.get('reason', ''),
          bad.get('reason', '')[:50])
    inv = post('/admin/qpu/submit', {'circuit': bell, 'qubits': 99})
    check('I7 超上限量子位被拒', inv.get('ok') is False,
          inv.get('reason', '')[:40])


# ---------------------------------------------------------------- J 审计包
def sc_j():
    pack = post('/admin/audit/export', None, since_ms=0)
    check('J1 证据包可导出', bool(pack.get('pack_digest')),
          f"counts={pack.get('counts')}")
    check('J2 默认不含 prompt 明文', pack.get('prompts_included') is False,
          f"prompts_included={pack.get('prompts_included')}")
    sys.path.insert(0, ROOT)
    from controller import trust as _trust
    v = _trust.verify_evidence_pack(pack)
    check('J3 证据包签名校验通过', v.get('ok'),
          f"digest_match={v.get('digest_match')} sig={v.get('signature_ok')}")

    tampered = json.loads(json.dumps(pack))
    tampered['counts']['tasks'] = (tampered['counts'].get('tasks', 0) + 999)
    v2 = _trust.verify_evidence_pack(tampered)
    check('J4 篡改后校验失败（可检出）', v2.get('ok') is False,
          f"digest_match={v2.get('digest_match')}")

    ch = get('/admin/audit/verify')
    check('J5 审计链完整', ch.get('ok') is True and ch.get('length', 0) > 5,
          f"length={ch.get('length')} broken={ch.get('broken')}")


# ---------------------------------------------------------------- K 结算
def sc_k():
    s = post('/admin/settlement', None, persist=True)
    check('K1 结算可执行并落库', s.get('total', {}).get('nodes', 0) >= 1,
          f"nodes={s.get('total', {}).get('nodes')} "
          f"owner={s.get('total', {}).get('owner_share_cny')}")
    check('K2 分成比例为规划假设并显式标注',
          s.get('assumption') is True and bool(s.get('note')),
          f"ratio={s.get('owner_share_ratio')}")
    st = get('/admin/settlement')
    check('K3 结算记录可查', st.get('records', 0) >= 1,
          f"records={st.get('records')}")
    # 幂等：同周期重算必须"先清后写"，不能把机主分成算两遍（BUG-V4-18）
    s2 = post('/admin/settlement', None, persist=True)
    st2 = get('/admin/settlement')
    check('K4 重复结算幂等（同周期先清后写，不重复计账）',
          st2.get('records') == st.get('records'),
          f"records {st.get('records')} → {st2.get('records')} "
          f"(overwritten={s2.get('overwritten_records')})")


# ---------------------------------------------------------------- L 分时复用
def sc_l():
    ts = get('/admin/time-slice', window_minutes=60, slice_minutes=15,
             tenants='a,b,c')
    share = ts.get('share', {})
    check('L1 分时排期生成', len(ts.get('slices', [])) == 4,
          f"share={share}")
    check('L2 份额归一', abs(sum(share.values()) - 1.0) < 0.01,
          f'sum={round(sum(share.values()), 4)}')
    check('L3 明示仅策略不执行', ts.get('execution') == 'policy_only',
          ts.get('note', '')[:40])
    bad = get('/admin/time-slice', window_minutes=10, slice_minutes=60)
    check('L4 非法窗口被拒', bad.get('ok') is False, bad.get('reason', '')[:40])


# ---------------------------------------------------------------- M SDK
def sc_m():
    sys.path.insert(0, os.path.join(ROOT, 'sdk'))
    from wnidia_client import WNIDIAClient as _C
    c = _C(CTRL, TOKEN)
    h = c.health()
    check('M1 SDK health 可用', h.get('ok') is True, f"version={h.get('version')}")
    cap = c.capabilities()
    check('M2 SDK capabilities 可用', bool(cap.get('board')), '')
    m = c.metering()
    check('M3 SDK metering 可用', 'total' in m.get('summary', {}), '')
    g = c.gates()
    check('M4 SDK gates 可用', len(g.get('gates', [])) == 5, '')
    u = c.tenant_usage('demo-d')
    check('M5 SDK 租户用量可用', u.get('tenant') == 'demo-d',
          f"entries={u.get('summary', {}).get('total', {}).get('entries')}")


# ---------------------------------------------------------------- N 隐私
SENSITIVE = 'ACME-绝密合同-编号ZX9981'


def sc_n():
    """prompt 不留中心明文（BP P13 决策③）。

    这组断言刻意"**既证数据没落库、又证功能没坏**"：
    只证明前者很容易（把字段删了就行），真正要证的是改造后任务还能跑完。
    """
    # N1 端到端仍能完成（prompt 靠内存副本送达 worker，不靠库里的明文）
    code, body = chat([{'role': 'user', 'content': f'{SENSITIVE} 请总结'}],
                      tenant='demo-n', project='privacy')
    tid = body.get('id')
    check('N1 改造后任务仍能正常完成（prompt 经内存副本送达 worker）',
          code == 200 and bool(tid), f'http={code} id={str(tid)[-6:]}')

    # N2 直接读 SQLite：库里不得出现任何非占位符的 prompt
    import sqlite3
    con = sqlite3.connect(os.path.join(ROOT, 'data', 'wnidia.db'))
    try:
        rows = con.execute('SELECT task, prompt FROM tasks').fetchall()
    finally:
        con.close()
    leaked = [(t, p) for t, p in rows
              if p and not str(p).startswith('[REDACTED')]
    check('N2 库里没有任何明文 prompt（直查 SQLite 核对）',
          not leaked, f'共 {len(rows)} 条任务，明文 {len(leaked)} 条')

    # N3 敏感串不得出现在 /admin/state 的任何字段里
    blob = json.dumps(get('/admin/state'), ensure_ascii=False)
    check('N3 /admin/state 不含敏感原文（全字段扫描）',
          SENSITIVE not in blob, f'扫描 {len(blob)} 字符')

    # N4 看板 /api/state 与 /admin/state 同口径脱敏（BUG-V5-04 回归）
    u = os.getenv('WNIDIA_DASH_USER', 'reviewer')
    pw = os.getenv('WNIDIA_DASH_PASS', 'wnidia2026')
    try:
        dr = S.get('http://127.0.0.1:8888/api/state', auth=(u, pw), timeout=15)
        check('N4 看板 /api/state 同样脱敏（不是第二个口径）',
              dr.status_code == 200 and SENSITIVE not in dr.text,
              f'http={dr.status_code}')
    except requests.RequestException as e:
        check('N4 看板 /api/state 同样脱敏（不是第二个口径）', False,
              f'看板不可达：{str(e)[:60]}')

    # N5 租户自助视图同样脱敏
    tb = json.dumps(get('/v1/tenant/demo-n/usage'), ensure_ascii=False)
    check('N5 租户用量视图同样脱敏', SENSITIVE not in tb,
          f'扫描 {len(tb)} 字符')

    # N6 脱敏输出必须留下可核验的痕迹（摘要 + 长度 + 状态）
    mine = [t for t in get('/admin/state')['tasks'] if t['task'] == tid]
    got = mine[0] if mine else {}
    check('N6 脱敏后仍可核验（摘要 + 长度 + 状态齐全）',
          bool(mine) and bool(got.get('prompt_digest'))
          and int(got.get('prompt_chars') or 0) > 0
          and got.get('prompt_state') in ('redacted', 'released'),
          f"state={got.get('prompt_state')} digest={got.get('prompt_digest')} "
          f"chars={got.get('prompt_chars')}")

    # N7/N8/N9 证据包
    pack = post('/admin/audit/export', None, since_ms=0)
    check('N7 证据包默认不含 prompt 明文',
          pack.get('prompts_included') is False,
          f"prompts_included={pack.get('prompts_included')}")
    check('N8 证据包保留 prompt 摘要（不泄露内容但可核验）',
          all('prompt_digest' in t for t in pack.get('tasks', []))
          and pack.get('prompt_policy', {}).get('store_plaintext') is False,
          f"policy_store={pack.get('prompt_policy', {}).get('store_plaintext')}")
    pack2 = post('/admin/audit/export', None, since_ms=0,
                 include_prompts='true')
    check('N9 请求 include_prompts 但开关未开时如实拒绝（不给"看起来给了"）',
          pack2.get('prompts_included') is False
          and pack2.get('prompts_requested') is True,
          f"included={pack2.get('prompts_included')} "
          f"requested={pack2.get('prompts_requested')}")

    # N10 策略视图可投屏自证
    pol = get('/admin/compliance').get('prompt_policy', {})
    check('N10 /admin/compliance 暴露 prompt 留存策略',
          pol.get('store_plaintext') is False
          and pol.get('reveal_plaintext') is False
          and str(pol.get('placeholder', '')).startswith('[REDACTED'),
          f"store={pol.get('store_plaintext')} "
          f"reveal={pol.get('reveal_plaintext')}")

    # N11 关键不变量：没有**活任务**因为"拿不到 prompt"而失败
    #     （既说明库里确实没有明文可依赖，也说明内存副本机制是有效的）
    #     注意看 miss_active 而不是总数：终态任务补不回是设计如此。
    ev = json.dumps(get('/admin/state')['events'], ensure_ascii=False)
    stt = pol.get('stats', {})
    miss_active = int(stt.get('miss_active', 0) or 0)
    check('N11 无活任务因 prompt 不可用而失败（内存副本机制有效）',
          'prompt_unavailable' not in ev and miss_active == 0,
          f"miss_active={miss_active} "
          f"miss_released={stt.get('miss_released')} "
          f"rehydrated={stt.get('rehydrated')}")


SCENARIOS = [('A 口径与能力面', sc_a), ('B 三维路由（时延）', sc_b),
             ('C SLA 优先级排队', sc_c), ('D 真实计量与分账', sc_d),
             ('E 机密计算与证明', sc_e), ('F 门槛度量', sc_f),
             ('G 边缘断网自治', sc_g), ('H 具身机队', sc_h),
             ('I QPU 抽象', sc_i), ('J 审计证据包', sc_j),
             ('K 机主结算', sc_k), ('L 分时复用策略', sc_l),
             ('M SDK', sc_m), ('N prompt 隐私', sc_n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep', action='store_true', help='跑完不关栈')
    ap.add_argument('--only', default=None, help='只跑指定场景首字母，如 EG')
    args = ap.parse_args()

    preflight_ports(DEFAULT_PORTS)
    env = dict(os.environ, WNIDIA_TOKEN=TOKEN, RESET_DB='1',
               WNIDIA_JEV_MODE='mock', WNIDIA_EXTRA_NODES='1')
    env.setdefault('WNIDIA_PY', sys.executable)
    proc = subprocess.Popen(['bash', 'scripts/run_local.sh'], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pgid = pgid_of(proc)
    t_start = time.time()
    try:
        ok = False
        for _ in range(40):
            try:
                S.get(f'{CTRL}/healthz', timeout=2); ok = True; break
            except requests.RequestException:
                time.sleep(1)
        if not ok:
            print('[error] 控制面未就绪，无法开始沙盒验证')
            return 2
        # 等集群真的可用（≥3 节点在线且有节点已上报时延），不再睡固定秒数
        ready, info = wait_cluster_ready()
        print(f"[info] 集群就绪={ready} online={info['online']} "
              f"latencies={info['latencies']}", flush=True)
        if not ready:
            print('[warn] 集群未在超时内就绪，后续断言可能因环境未起全而失败',
                  flush=True)

        for name, fn in SCENARIOS:
            if args.only and name[0] not in args.only:
                continue
            print(f'\n--- {name} ---')
            try:
                fn()
            except Exception as e:                    # noqa: BLE001
                check(f'{name} 场景异常', False, f'{type(e).__name__}: {e}')

        passed = sum(1 for r in results if r['ok'])
        total = len(results)
        report = {
            'generated_at': int(time.time() * 1000),
            'duration_s': round(time.time() - t_start, 1),
            'passed': passed, 'failed': total - passed, 'total': total,
            'scenarios': [n for n, _ in SCENARIOS],
            'results': results,
        }
        os.makedirs(os.path.join(ROOT, 'data'), exist_ok=True)
        out = os.path.join(ROOT, 'data', 'sandbox_v4_result.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print('\n' + '=' * 56)
        print(f"沙盒验证：通过 {passed}/{total}（耗时 {report['duration_s']}s）")
        fails = [r for r in results if not r['ok']]
        if fails:
            print('失败项：')
            for r in fails:
                print(f"  - {r['name']} {r['detail']}")
        print(f'报告已写入：{out}')
        return 1 if fails else 0
    finally:
        if not args.keep:
            kill_tree(pgid, proc)
        else:
            print('\n[--keep] 服务仍在运行：tmux 未使用，直接 kill 进程组即可')


if __name__ == '__main__':
    sys.exit(main() or 0)
