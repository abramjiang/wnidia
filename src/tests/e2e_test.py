# -*- coding: utf-8 -*-
"""WNIDIA 端到端自检：全新启动本地栈，验证网关与三条韧性路径。

用法：
    WNIDIA_PY=/path/to/python python tests/e2e_test.py
    EVAL_PYTHON=... 亦可（与 run_evals 保持一致）
退出码 0 表示全部通过。

本轮修复：
1. 网关调用此前**未携带 Authorization**，而 /v1/chat/completions 早已加了鉴权，
   所以一旦真跑起来必然 401 并在取 ['choices'] 时抛 KeyError——测试与实现脱节。
2. 覆盖新增能力：引擎可插拔（/admin/engines、节点 engine 字段）、
   合规自检（/admin/compliance）、engine-selector Skill 路由。
3. 密钥改强口令，避免依赖"回环可弱口令"的豁免路径。
"""
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
TOKEN = 'wnidia-e2e-token-7c1f9a3d'
results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def wait_health(url, timeout=30):
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=3); return True
        except Exception:
            time.sleep(1)
    return False


def _hdr():
    return {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}


def admin(path, method='POST'):
    s = robust_session()   # 连接层重试
    r = s.request(method, f'http://127.0.0.1:9000{path}', headers=_hdr(),
                  timeout=25)
    return r.json()


def gateway(body, timeout=40):
    s = robust_session()   # 连接层重试
    return s.post('http://127.0.0.1:9000/v1/chat/completions', json=body,
                  headers=_hdr(), timeout=timeout)


def state():
    r = robust_session()   # 连接层重试
    resp = r.get('http://127.0.0.1:9000/admin/state', headers=_hdr(), timeout=25)
    if resp.status_code != 200:
        # 端口被旧实例占用时 Token 不匹配会得到 401，原实现会抛出与真实原因
        # 无关的 KeyError('tasks')；这里改成可操作的明确报错。
        raise SystemExit(
            f'[error] /admin/state 返回 {resp.status_code}。'
            f'通常是端口被上一次未清理的实例占用（Token 不匹配）。'
            f'请先执行 tests/_clean.py 的 free_ports 或手工 lsof -ti:9000 | xargs kill -9')
    return resp.json()


def main():
    preflight_ports(DEFAULT_PORTS)
    env = dict(os.environ, WNIDIA_TOKEN=TOKEN, RESET_DB='1',
               WNIDIA_JEV_MODE='mock')
    env.setdefault('WNIDIA_PY', sys.executable)
    proc = subprocess.Popen(['bash', 'scripts/run_local.sh'], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pgid = pgid_of(proc)      # pid 之后会被回收，必须先记下进程组
    try:
        check('控制面健康', wait_health('http://127.0.0.1:9000/healthz'))
        time.sleep(9)

        # 0) 鉴权必须生效（手册 8.2-9）
        s = robust_session()   # 连接层重试
        r = s.post('http://127.0.0.1:9000/v1/chat/completions',
                   json={'messages': [{'role': 'user', 'content': 'hi'}]},
                   timeout=10)
        check('无 Token 被拒', r.status_code == 401, f'http={r.status_code}')

        st = state()
        check('3 节点在线', sum(n['status'] == 'online' for n in st['nodes']) == 3,
              f"实际 {[n['status'] for n in st['nodes']]}")
        check('节点带引擎画像',
              all(n.get('engine') for n in st['nodes']),
              f"engines={[n.get('engine') for n in st['nodes']]}")

        # 0.5) 引擎可插拔层
        eng = admin('/admin/engines?probe=false', 'GET')
        check('引擎目录可枚举',
              set(eng.get('catalog', {})) >= {'mock', 'ollama', 'vllm',
                                              'tensorfold'},
              f"catalog={sorted(eng.get('catalog', {}))}")
        check('引擎选路可解释',
              bool(eng.get('decision', {}).get('selected'))
              and bool(eng['decision'].get('reason')),
              f"selected={eng.get('decision', {}).get('selected')}")

        # 0.6) 合规自检
        comp = admin('/admin/compliance', 'GET')
        check('合规自检可查', comp.get('token_strength_ok') is True
              and list(comp.get('public_ports', [])) == [8888, 9000],
              f"token_ok={comp.get('token_strength_ok')}")

        # 1) 普通问答
        r = gateway({'messages': [{'role': 'user', 'content': '你好，解释下算力调度'}]})
        ok = False
        try:
            ok = r.status_code == 200 and bool(
                r.json()['choices'][0]['message']['content'])
        except Exception:
            ok = False
        check('网关问答', ok, f'http={r.status_code}')

        # 2) 作弊 + 多数决
        admin('/admin/node/cheat?node=cpu-1&on=true')
        r = gateway({'messages': [{'role': 'user', 'content': '请校验这个关键结论'}],
                     'verify': True})
        st = state()
        cpu = [n for n in st['nodes'] if n['node'] == 'cpu-1'][0]
        flagged = any('不一致' in e['message'] for e in st['events']
                      if e['kind'] == 'verify')
        check('多数决识别作弊', flagged and cpu['reputation'] < 1.0,
              f"rep={cpu['reputation']} flagged={flagged}")

        # 3) 抢占 + 自动恢复
        try:
            gateway({'messages': [{'role': 'user',
                    'content': '批量处理一批较长的素材需要运行一段时间 xxxx yyyy zzzz aaaa bbbb'}],
                    'task_type': 'batch', 'sla': 'sla-3'}, timeout=2)
        except requests.exceptions.ReadTimeout:
            pass
        time.sleep(3)
        inj = admin('/admin/inject/preempt')
        check('高优抢占 spot', inj.get('ok'), inj.get('message', ''))
        time.sleep(12)
        st = state()
        resumed = any('恢复' in e['message'] for e in st['events'])
        hi_done = any(t['task'].startswith('hi-') and t['state'] == 'done'
                      for t in st['tasks'])
        check('抢占后自动恢复', resumed and hi_done,
              f'resume={resumed} hi_done={hi_done}')
        stuck = [t['task'] for t in st['tasks'] if t['state'] == 'binding']
        check('无任务卡在 binding', not stuck, f'stuck={stuck}')

        # 4) 掉线重路由 + 隔离期内不被心跳复活
        admin('/admin/node/lost?node=cpu-1')
        time.sleep(7)
        st = state()
        cpu = [n for n in st['nodes'] if n['node'] == 'cpu-1'][0]
        check('掉线事件', any(e['kind'] == 'lost' and 'cpu-1' in e['message']
                              for e in st['events']))
        check('隔离期内保持离线', cpu['status'] == 'lost',
              f"status={cpu['status']}")

        # 5) Agent 应用层（仅回环）
        try:
            r = requests.get('http://127.0.0.1:7000/healthz', timeout=5)
            skills = r.json().get('skills', [])
        except Exception as e:
            skills = []
            print(f'  (agent 未就绪：{e})')
        # v4：Skill 从 5 个扩到 9 个。原断言写的是 ">= 5" 却把用例名叫成
        # "暴露 5 个"，名字与判据不一致容易误导；这里显式校验 v4 的 9 个名字。
        EXPECTED_SKILLS = {
            'smart-dispatcher', 'gpu-slicer', 'resource-doctor', 'idle-onboarding',
            'engine-selector', 'edge-box-provisioner', 'private-cloud-planner',
            'gate-inspector', 'cc-auditor'}
        missing = sorted(EXPECTED_SKILLS - set(skills))
        check(f'Agent 暴露全部 {len(EXPECTED_SKILLS)} 个 Skill',
              not missing, f'missing={missing}' if missing else f'共 {len(skills)} 个')

        try:
            r = requests.post('http://127.0.0.1:7000/v1/agent/chat',
                              json={'message': '这个机密长文本任务该用哪个引擎？',
                                    'use_llm': False},
                              headers={'Authorization': f'Bearer {TOKEN}'},
                              timeout=60)
            j = r.json()
            check('Agent 路由到 engine-selector',
                  j.get('skill') == 'engine-selector',
                  f"skill={j.get('skill')} source={j.get('source')}")
        except Exception as e:
            check('Agent 路由到 engine-selector', False, str(e)[:80])
    finally:
        kill_tree(pgid, proc)

    failed = [r for r in results if not r[1]]
    print('\n' + '='.ljust(40, '='))
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    if failed:
        print('失败项：')
        for name, _, detail in failed:
            print(f'  - {name} {detail}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
