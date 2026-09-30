# -*- coding: utf-8 -*-
"""跨进程编排链路测试（修复 P2 跨进程 bug 的回归）。

背景：看板（dash，:8888）与 API（main，:9000）是两个进程，prompt 隐私
spool 在进程内存里。此前门户页的 /api/demo/run 在看板进程里直接跑编排器，
prompt 注册进了看板进程，而派发循环在 API 进程 → 一律 prompt_unavailable。

修复后 /api/demo/* 代理到 API 进程。本测试双进程起服务，从看板端口发起
run，断言任务在 API 进程被正确派发执行（不是 prompt_unavailable）。
"""
import json
import os
import sys
import threading
import time
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = '/tmp/xproc_demo.db'
for f in (DB, DB + '-wal', DB + '-shm'):
    if os.path.exists(f):
        os.remove(f)
os.environ['WNIDIA_DB'] = DB
os.environ['WNIDIA_MODE'] = 'mock'
os.environ['WNIDIA_TOKEN'] = 'xproc-token-0123456789'
os.environ['WNIDIA_API_PORT'] = '9601'
os.environ['WNIDIA_DASH_PASS'] = 'xproc-pass-abcdef'
os.environ['WNIDIA_HOST'] = '127.0.0.1'   # 测试绑回环，绕开公网端口合规闸门

from controller import db, registry                 # noqa: E402
from controller.models import NodeProfile, NodeStatus  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[PASS]', name, detail)
    else:
        FAIL += 1
        print('[FAIL]', name, detail)


def boot(mod, port):
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(mod.app, host='127.0.0.1',
                                        port=port, log_level='error'))
    threading.Thread(target=srv.run, daemon=True).start()


def http(method, url, auth=None, headers=None, timeout=15):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    if auth:
        import base64
        tok = base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
        req.add_header('Authorization', 'Basic ' + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:      # noqa: BLE001
            return e.code, {}


def main():
    db.reset_db()
    now = int(time.time() * 1000)
    for name, role, tier, mem in (('cloud-0', 'prefill', 'cloud', 40),
                                  ('edge-1', 'decode', 'edge', 8),
                                  ('cpu-1', 'cpu', 'cpu', 4)):
        registry.register(NodeProfile(
            node=name, role=role, tier=tier, mem_total_gb=mem,
            mem_limit_gb=mem, vllm_port=8100 + int(name[-1]) + 1,
            sovereign_region='local', status=NodeStatus.ONLINE.value,
            util=20.0, free_mem_gb=mem - 1, kv_hit=0.5, reputation=1.0,
            last_heartbeat=now, trusted=True))

    # 双进程：API 9601（编排器真正执行处）+ 看板 9602（门户入口）
    from controller import main as api_mod
    from controller import dash as dash_mod
    boot(api_mod, 9601)
    boot(dash_mod, 9602)

    # 测试环境没有真 worker：起一个心跳线程防止 harness 把节点判离线
    # （生产上 worker 每 2s 心跳；不模拟的话 15s 后节点全部 lost，任务必被拒）
    STOP = threading.Event()

    def _hb():
        from controller import registry as _rg
        while not STOP.is_set():
            for n in ('cloud-0', 'edge-1', 'cpu-1'):
                try:
                    _rg.heartbeat(n, util=25.0, free_mem_gb=6.0, kv_hit=0.5,
                                  latency_ms=2.0)
                except Exception:      # noqa: BLE001
                    pass
            STOP.wait(2.0)

    threading.Thread(target=_hb, daemon=True).start()
    time.sleep(2.0)

    B = 'http://127.0.0.1:9602'
    AUTH = ('reviewer', 'xproc-pass-abcdef')

    # 1) 从看板端口跑 route 场景（此前这条链路必现 prompt_unavailable）
    code, j = http('POST', f'{B}/api/demo/run?scene=route', auth=AUTH)
    check('看板端口启动场景', code == 200 and j.get('ok') is True, (code, j))

    deadline = time.time() + 60
    st = {}
    while time.time() < deadline:
        code, st = http('GET', f'{B}/api/demo/status', auth=AUTH)
        if st.get('status') in ('done', 'stopped'):
            break
        time.sleep(0.5)
    check('场景跑完', st.get('status') == 'done', st.get('status'))
    check('三步全部执行', len(st.get('steps') or []) == 3, len(st.get('steps') or []))

    # 2) 关键回归：无 prompt_unavailable；每个任务要么真实派发（有 node），
    #    要么有可解释的拒绝/失败原因（测试环境无真 worker，failed 合法）
    steps = st.get('steps', [])
    check('无 prompt_unavailable（跨进程修复生效）',
          not any('prompt_unavailable' in json.dumps(s.get('result') or {},
                                                    ensure_ascii=False)
                  for s in steps))
    states_ok = all((s.get('result') or {}).get('state')
                    in ('done', 'failed', 'rejected') for s in steps)
    check('每步任务都到达终态', states_ok,
          [(s['id'], (s.get('result') or {}).get('state')) for s in steps])

    # 3) DB 证据：派发过（有 node）或拒绝/失败有原因（error 非空），二必有其一
    from controller import db as _db
    ts = [t for t in _db.all_tasks() if t.tenant == 'demo-scene']
    check('DB 中任务「已派发或有解释」',
          all((t.node or t.error) for t in ts),
          [(t.task, t.state, t.node, t.error) for t in ts])
    check('DB 中没有占位符派发',
          not any(t.error == 'prompt_unavailable' for t in ts))

    # 4) 运行中再启动：返回 200 + 明确文案（门户能显示，不再盲弹"启动失败"）
    from controller import demo as _demo
    _demo.RUN['status'] = 'running'
    code, j = http('POST', f'{B}/api/demo/run?scene=edge', auth=AUTH)
    check('运行中再启动返回可读文案',
          code == 200 and not j.get('ok') and '已有场景在运行' in (j.get('error') or ''),
          (code, j))
    _demo.RUN['status'] = 'idle'

    print('\n========================================')
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
