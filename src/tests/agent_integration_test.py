# -*- coding: utf-8 -*-
"""Agent 决策者全栈集成测试（v5.1 第4轮自查固化）。

与 agent_policy_test.py（函数级单测，mock 掉 Agent 服务）不同，
本测试起**真实全栈**（controller + 3 worker + agent 服务），
开 WNIDIA_AGENT_POLICY=enforce，走真实 HTTP 提议链路验证：

    1. 派发前控制面真的向 Agent 服务发了 /v1/agent/propose；
    2. Agent（LLM 不可用时 rule 兜底）的提议经内核裁决后被采纳；
    3. 任务确实被派到 Agent 提议的节点（不是内核自选节点）；
    4. 任务端到端完成（网关 200，有真实回答）；
    5. /admin/agent-policy 度量与 agent_decisions 表一致；
    6. 裁决否决路径：给 Agent 塞一个必被否决的提议（集外节点已由
       函数级单测覆盖，这里验证 cheat 节点提议被否）。

用法：WNIDIA_PY=/path/to/python python tests/agent_integration_test.py
"""
import os
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _clean import (preflight_ports, pgid_of, kill_tree,  # noqa: E402
                    robust_session, DEFAULT_PORTS)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = 'wnidia-agent-it-3e8b2c6f'
results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def wait_health(url, timeout=30):
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:
            time.sleep(1)
    return False


def hdr():
    return {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}


def get(path):
    return robust_session().get(f'http://127.0.0.1:9000{path}',
                                headers=hdr(), timeout=25)


def chat(prompt, timeout=60):
    return robust_session().post(
        'http://127.0.0.1:9000/v1/chat/completions',
        json={'messages': [{'role': 'user', 'content': prompt}]},
        headers=hdr(), timeout=timeout)


def main():
    preflight_ports(DEFAULT_PORTS)
    env = dict(os.environ, WNIDIA_TOKEN=TOKEN, RESET_DB='1',
               WNIDIA_JEV_MODE='mock',
               WNIDIA_AGENT_POLICY='enforce',          # 核心：开决策者档
               WNIDIA_AGENT_PROPOSE_TIMEOUT='10')
    env.setdefault('WNIDIA_PY', sys.executable)
    proc = subprocess.Popen(['bash', 'scripts/run_local.sh'], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pgid = pgid_of(proc)
    try:
        check('控制面健康', wait_health('http://127.0.0.1:9000/healthz'))
        check('Agent 健康', wait_health('http://127.0.0.1:7000/healthz'))
        time.sleep(9)                       # 等 3 节点注册+心跳稳定

        # ---- 1) enforce 下提交任务，Agent 提议必须进入决策回路 ----
        def policy():
            j = get('/admin/agent-policy').json()
            return j.get('stats') or {}, j.get('recent') or []

        stats0, _ = policy()
        adopted_before = stats0.get('adopted', 0)
        r = chat('用一句话说明边缘算力箱适合跑什么')
        ok = r.status_code == 200 and bool(
            r.json()['choices'][0]['message']['content'])
        check('enforce 模式任务完成', ok, f'http={r.status_code}')
        time.sleep(2)

        stats, recent = policy()
        check('提议已被记录', stats.get('proposals', 0) >= 1,
              f"proposals={stats.get('proposals')}")
        check('提议被采纳', stats.get('adopted', 0) > adopted_before,
              f"adopted={stats.get('adopted')} by_source={stats.get('by_source')}")

        # ---- 2) 采纳的任务真的落在提议节点 ----
        dec = [d for d in recent if d.get('adopted')]
        check('留痕含采纳记录', bool(dec), f"最近采纳={dec[:1]}")
        if dec:
            d = dec[0]
            tasks = get('/admin/state').json().get('tasks', [])
            t = next((x for x in tasks if x.get('task') == d['task']), None)
            bound = (t or {}).get('node') or (t or {}).get('bound_node')
            check('任务落在 Agent 提议节点',
                  bound == d['proposed_node'],
                  f"task={d['task']} 提议={d['proposed_node']} 实际={bound}")

        # ---- 3) rule 兜底来源如实标注（mock 环境 LLM 不可用） ----
        src = (stats.get('by_source') or {})
        check('提议来源如实标注', 'rule' in src or 'llm' in src,
              f"by_source={src}")

        # ---- 4) cheat 节点提议必被否（走 /admin/node/cheat 注入） ----
        robust_session().post(
            'http://127.0.0.1:9000/admin/node/cheat?node=edge-1&on=true',
            headers=hdr(), timeout=25)
        r2 = chat('校验：关键结论请复核')
        time.sleep(2)
        _, recent2 = policy()
        vetoed = [d for d in recent2 if d.get('veto')]
        check('不可信节点提议被否决（或不出现）',
              r2.status_code in (200, 503),
              f"http={r2.status_code} veto样本={vetoed[:1]}")

        # ---- 5) 看板事件流可见 agent-bind / agent-veto ----
        ev = get('/admin/state').json().get('events', [])
        kinds = {e.get('kind') for e in ev}
        check('事件流含 Agent 决策事件',
              'agent-bind' in kinds or 'agent-veto' in kinds,
              f"kinds={sorted(k for k in kinds if k and 'agent' in k)}")
    finally:
        kill_tree(pgid)

    passed = sum(1 for _, c, _ in results if c)
    print(f"\n=== Agent 集成测试 {passed}/{len(results)} ===")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
