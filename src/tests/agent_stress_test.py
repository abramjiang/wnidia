# -*- coding: utf-8 -*-
"""Agent 决策回路压力与异常注入测试（v5.1 第5轮自查固化）。

覆盖第4轮未触达的五类风险：
    1. 并发：10 个任务同时走 enforce 提议，留痕数 == 派发数，无重复/丢失；
    2. 异常注入：Agent 返回畸形 JSON / HTTP 500 / 响应体缺字段；
    3. 慢 Agent：提议接口 sleep 超过 AGENT_PROPOSE_TIMEOUT，
       控制面必须在超时附近快速回落，不能拖垮 2s 派发节拍；
    4. 三档热切换：off→shadow→enforce→off，观察留痕行为逐档正确；
    5. 留痕持久化：shadow 线程写完后重启（重开 db 连接），数据仍在。

用法：WNIDIA_PY=/path/to/python python tests/agent_stress_test.py
"""
import json
import os
import sqlite3
import sys
import threading
import time
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['WNIDIA_DB'] = '/tmp/agent_stress.db'
if os.path.exists('/tmp/agent_stress.db'):
    os.remove('/tmp/agent_stress.db')

from fastapi import FastAPI, Request  # noqa: E402
import uvicorn  # noqa: E402

from controller import db, agent_policy, scheduler, registry, config  # noqa: E402
from controller.models import (TaskSpec, NodeProfile, NodeStatus,  # noqa: E402
                               SLA, Secret)

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def mk_task(i):
    return TaskSpec(task=f't-stress-{i}', tenant='demo', prompt='hi',
                    task_type='chat', tokens_in=10, need_mem_gb=1, cost=1,
                    sovereign_region='local', secret=Secret.L1,
                    sla=SLA.S2.value)


def seed_nodes():
    db.reset_db()
    now = int(time.time() * 1000)
    for name, role, tier, mem in [('cloud-0', 'prefill', 'cloud', 40),
                                  ('edge-1', 'decode', 'edge', 8),
                                  ('cpu-1', 'cpu', 'cpu', 4)]:
        registry.register(NodeProfile(
            node=name, role=role, tier=tier, mem_total_gb=mem,
            mem_limit_gb=mem, vllm_port=8101, sovereign_region='local',
            status=NodeStatus.ONLINE.value, util=30.0, free_mem_gb=mem - 1,
            kv_hit=0.5, reputation=1.0, last_heartbeat=now, trusted=True))


# ---------------------------------------------------------------- 假 Agent
fake = FastAPI()
BEHAVIOR = {'mode': 'ok', 'delay': 0.0}


@fake.post('/v1/agent/propose')
def propose(req: dict):
    # sync def：FastAPI 放入线程池执行，慢请求不堵事件循环——
    # 与产品端 agent/app.py 的 propose 路由行为一致（此前用 async def +
    # time.sleep 会堵死事件循环，是假服务自身的实现错误，不是产品缺陷）。
    if BEHAVIOR['delay']:
        time.sleep(BEHAVIOR['delay'])
    m = BEHAVIOR['mode']
    node = req['candidates'][0]['node'] if req.get('candidates') else 'x'
    if m == 'ok':
        return {'target_node': node, 'reason': 't', 'source': 'llm'}
    if m == 'bad_json':
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse('not-json{{{', status_code=200)
    if m == 'http500':
        from fastapi.responses import JSONResponse
        return JSONResponse({'err': 1}, status_code=500)
    if m == 'missing_field':
        return {'reason': 'no target_node'}
    return {'target_node': node, 'source': 'llm'}


def start_fake(port):
    srv = uvicorn.Server(uvicorn.Config(fake, host='127.0.0.1', port=port,
                                        log_level='error'))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(1.2)


def main():
    seed_nodes()
    config.API_TOKEN = 'stress-token'
    config.AGENT_PROPOSE_TIMEOUT = 2          # 压短超时让慢 Agent 用例跑得快
    start_fake(7750)
    config.AGENT_BASE = 'http://127.0.0.1:7750'

    # ---- 1) 并发 enforce：10 任务同时提议 ----
    config.AGENT_POLICY = 'enforce'
    BEHAVIOR.update(mode='ok', delay=0)
    with cf.ThreadPoolExecutor(10) as ex:
        outs = list(ex.map(lambda i: agent_policy.decide(mk_task(i)),
                           range(10)))
    time.sleep(1)
    st = db.agent_policy_stats()
    check('并发10提议全部留痕', st['proposals'] == 10,
          f"proposals={st['proposals']}")
    check('并发下采纳结果合法', all(o in (None, 'cloud-0', 'edge-1', 'cpu-1')
                                  for o in outs),
          f"outs={set(outs)}")

    # ---- 2) 异常注入：畸形 JSON / 500 / 缺字段 → 全部安全回落 ----
    db.reset_db(); seed_nodes()
    for mode_name in ('bad_json', 'http500', 'missing_field'):
        BEHAVIOR.update(mode=mode_name, delay=0)
        t0 = time.time()
        out = agent_policy.decide(mk_task(99))
        dt = time.time() - t0
        check(f'异常[{mode_name}]快速回落',
              out is None and dt < config.AGENT_PROPOSE_TIMEOUT + 1,
              f'{dt:.2f}s')

    # ---- 3) 慢 Agent：响应 5s > 超时 2s，主循环不受拖累 ----
    BEHAVIOR.update(mode='ok', delay=5)
    t0 = time.time()
    out = agent_policy.decide(mk_task(100))
    dt = time.time() - t0
    check('慢Agent按超时回落', out is None and dt < 3.5, f'{dt:.2f}s')
    BEHAVIOR.update(mode='ok', delay=0)

    # ---- 4) 三档热切换 ----
    db.reset_db(); seed_nodes()
    config.AGENT_POLICY = 'off'
    agent_policy.decide(mk_task(1))
    check('off 档零留痕', db.agent_policy_stats()['proposals'] == 0)

    config.AGENT_POLICY = 'shadow'
    assert agent_policy.decide(mk_task(2)) is None
    for _ in range(30):                      # 后台线程写库，最多等 3s
        if db.agent_policy_stats()['proposals']:
            break
        time.sleep(0.1)
    st = db.agent_policy_stats()
    check('shadow 只留痕不采纳',
          st['proposals'] == 1 and st['adopted'] == 0,
          f"proposals={st['proposals']} adopted={st['adopted']}")

    config.AGENT_POLICY = 'enforce'
    prefer = agent_policy.decide(mk_task(3))
    st = db.agent_policy_stats()
    check('enforce 采纳并留痕',
          prefer is not None and st['adopted'] == 1,
          f"prefer={prefer} adopted={st['adopted']}")

    config.AGENT_POLICY = 'off'
    agent_policy.decide(mk_task(4))
    check('切回 off 后不再留痕', db.agent_policy_stats()['proposals'] == 2,
          f"proposals={db.agent_policy_stats()['proposals']}")

    # ---- 5) 留痕持久化：新连接重开库，数据仍在 ----
    n_before = db.agent_policy_stats()['proposals']
    c = sqlite3.connect(os.environ['WNIDIA_DB'])
    n_disk = c.execute('SELECT COUNT(*) FROM agent_decisions').fetchone()[0]
    c.close()
    check('留痕落盘可持久读取', n_disk == n_before,
          f'memory={n_before} disk={n_disk}')

    passed = sum(1 for _, c_, _ in results if c_)
    print(f"\n=== Agent 压力/异常测试 {passed}/{len(results)} ===")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
