# -*- coding: utf-8 -*-
"""Agent 决策策略（v5.1）单测：LLM 提议 → 内核裁决 → 采纳/回落。

覆盖四类关键行为：
1. off   ：零调用、零留痕（默认档绝对不改变现有行为）；
2. shadow：后台留痕、派发不被 LLM 时延阻塞（返回 None 且立即返回）；
3. enforce：候选集内提议被采纳；集外/宕机/坏 JSON 一律回落；
4. scheduler.schedule(prefer_node=)：非法 prefer 被内核自然忽略。

退出码 0 表示全部通过。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('WNIDIA_DB', '/tmp/wnidia_agent_policy_test.db')

from controller import agent_policy, config, db, registry, scheduler  # noqa: E402
from controller.models import (NodeProfile, NodeStatus, Secret, SLA,  # noqa: E402
                               TaskSpec)

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def fresh_env():
    if os.path.exists(os.environ['WNIDIA_DB']):
        os.remove(os.environ['WNIDIA_DB'])
    db.reset_db()
    for i, (name, role, tier, mem) in enumerate([
            ('cloud-0', 'prefill', 'cloud', 40),
            ('edge-1', 'decode', 'edge', 8)]):
        registry.register(NodeProfile(
            node=name, role=role, tier=tier, mem_total_gb=mem,
            mem_limit_gb=mem, vllm_port=8101 + i, sovereign_region='local',
            status=NodeStatus.ONLINE.value, util=30.0, free_mem_gb=mem - 1,
            kv_hit=0.5, reputation=1.0, last_heartbeat=int(time.time() * 1000),
            trusted=True))


def task(name):
    return TaskSpec(task=name, tenant='demo', prompt='你好', task_type='chat',
                    tokens_in=100, need_mem_gb=1, cost=1,
                    sovereign_region='local', secret=Secret.L1,
                    sla=SLA.S2.value)


def start_agent(port, handler):
    from fastapi import FastAPI
    import uvicorn
    app = FastAPI()
    app.post('/v1/agent/propose')(handler)
    srv = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                        log_level='error'))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(1.0)


def main():
    fresh_env()
    t1 = task('t-1')

    # ---- off ----
    config.AGENT_POLICY = 'off'
    check('off 零调用零留痕', agent_policy.decide(t1) is None
          and db.agent_policy_stats()['proposals'] == 0)

    # ---- shadow（后台留痕，不阻塞） ----
    def good(req: dict):
        return {'target_node': req['candidates'][1]['node'], 'reason': 'test',
                'confidence': 0.9, 'source': 'llm'}
    start_agent(7710, good)
    config.AGENT_BASE = 'http://127.0.0.1:7710'
    config.API_TOKEN = 'test-token'
    config.AGENT_POLICY = 'shadow'
    t0 = time.time()
    r = agent_policy.decide(t1)
    dt = time.time() - t0
    check('shadow 立即返回不阻塞', r is None and dt < 0.5, f'{dt:.2f}s')
    deadline = time.time() + 5
    while time.time() < deadline and db.agent_policy_stats()['proposals'] < 1:
        time.sleep(0.1)
    st = db.agent_policy_stats()
    check('shadow 后台完成留痕且不采纳',
          st['proposals'] == 1 and st['adopted'] == 0, str(st['agreement_rate']))

    # ---- enforce：合法提议被采纳 ----
    config.AGENT_POLICY = 'enforce'
    t2 = task('t-2')
    r = agent_policy.decide(t2)
    check('enforce 采纳候选集内提议', r == 'edge-1', str(r))

    # ---- enforce：集外提议被否 ----
    def bad(req: dict):
        return {'target_node': 'ghost-node', 'source': 'llm'}
    start_agent(7711, bad)
    config.AGENT_BASE = 'http://127.0.0.1:7711'
    t3 = task('t-3')
    d = None
    check('enforce 集外提议回落', agent_policy.decide(t3) is None
          and (d := db.agent_decisions(1)[0])['veto'] == 'agent_invalid',
          d['veto'] if d else '')

    # ---- enforce：服务宕机快速回落 ----
    config.AGENT_BASE = 'http://127.0.0.1:7999'
    t4 = task('t-4')
    t0 = time.time()
    ok = agent_policy.decide(t4) is None
    dt = time.time() - t0
    d = db.agent_decisions(1)[0]
    check('服务不可达快速回落', ok and dt < 3
          and d['veto'] == 'agent_unavailable', f'{dt:.2f}s {d["veto"]}')

    # ---- 坏 JSON ----
    def junk(req: dict):
        from fastapi import Response
        return Response(content='not-json', media_type='application/json')
    start_agent(7712, junk)
    config.AGENT_BASE = 'http://127.0.0.1:7712'
    t5 = task('t-5')
    check('坏 JSON 回落不抛异常', agent_policy.decide(t5) is None)

    # ---- scheduler 兜底 ----
    config.AGENT_POLICY = 'off'
    res = scheduler.schedule(t1, prefer_node='edge-1')
    check('schedule 采纳合法 prefer', res.node == 'edge-1')
    res2 = scheduler.schedule(t5, prefer_node='ghost')
    check('非法 prefer 被内核忽略', res2.node in ('cloud-0', 'edge-1'))

    # ---- 度量口径 ----
    st = db.agent_policy_stats()
    check('度量端点口径完整',
          0 <= (st['agreement_rate'] or 0) <= 1 and st['proposals'] >= 4
          and isinstance(st['veto_top'], list))

    failed = [n for n, ok in results if not ok]
    print(f'\n通过 {len(results) - len(failed)}/{len(results)}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
