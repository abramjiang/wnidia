# -*- coding: utf-8 -*-
"""场景编排器（P1）测试。

覆盖：场景清单完整性、真实执行、单步异常不中断、停止语义、
并发保护、Skill 真调、状态最终一致性（演示后节点必须复原）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = '/tmp/demo_test.db'
if os.path.exists(DB):
    os.remove(DB)
os.environ['WNIDIA_DB'] = DB
os.environ['WNIDIA_MODE'] = 'mock'

from controller import db, demo, registry            # noqa: E402
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


def register():
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


def wait_done(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if demo.status()['status'] in ('done', 'stopped'):
            return True
        time.sleep(0.3)
    return False


def main():
    db.reset_db()
    register()

    # 1) 场景清单
    sc = demo.scenes()
    check('场景清单含 5 个场景', len(sc) == 5, list(sc))
    check('每个场景都有步骤与此项目规划依据',
          all(s['steps'] and s['bp'].startswith('此项目') for s in sc.values()),
          {k: len(v['steps']) for k, v in sc.items()})

    # 2) 未知场景被拒
    r = demo.run('nope')
    check('未知场景被拒绝', r.get('ok') is False and '可选' in r, r.get('error'))

    # 3) 真实跑一遍 edge 场景（submit 打桩成瞬时返回，避免等真实推理）
    real_submit = demo.submit
    calls = {'n': 0}

    def fake_submit(*a, **kw):
        calls['n'] += 1
        return {'task': 'demo-fake-%d' % calls['n'], 'state': 'done',
                'node': 'edge-1', 'engine': 'mock', 'error': '',
                'progress': 1.0, 'latency_s': 0.1}

    demo.submit = fake_submit
    try:
        r = demo.run('edge')
        check('启动 edge 场景', r.get('ok') is True and r['steps'] == 6, r)
        check('场景在超时前跑完', wait_done(60))
        st = demo.status()
        check('6 步全部执行', len(st['steps']) == 6, len(st['steps']))
        check('6 步全部成功', all(s.get('ok') for s in st['steps']),
              [(s['id'], s.get('ok')) for s in st['steps']])
        check('每步都有真实结果',
              all(isinstance(s.get('result'), dict) for s in st['steps']))
        # 状态复原：演示结束 cloud-0 必须回到在线
        n = db.get_node('cloud-0')
        check('演示后中心云已复原在线', n.status == 'online', n.status)
        # 事件流写入
        kinds = [getattr(e, 'kind', None) or
                 (e.to_dict() if hasattr(e, 'to_dict') else {}).get('kind')
                 for e in db.recent_events(60)]
        check('事件流含 demo 步骤', 'demo' in kinds, kinds.count('demo'))
    finally:
        demo.submit = real_submit

    # 4) 单步异常不得中断整个场景
    orig = demo.SCENES['route']['build']

    def broken_build():
        steps = orig()
        steps[1]['fn'] = lambda: (_ for _ in ()).throw(RuntimeError('注入故障'))
        return steps

    demo.SCENES['route']['build'] = broken_build
    demo.submit = fake_submit          # 同样打桩，本例只验证容错编排
    try:
        demo.run('route')
        check('含故障步的场景仍能跑完', wait_done(40))
        st = demo.status()
        bad = [s for s in st['steps'] if not s.get('ok')]
        check('故障步被记为失败且有原因',
              len(bad) == 1 and 'RuntimeError' in (bad[0]['error'] or ''),
              bad[:1])
        check('故障步之后仍继续执行', len(st['steps']) == 3, len(st['steps']))
    finally:
        demo.SCENES['route']['build'] = orig
        demo.submit = real_submit

    # 5) stop 语义
    demo.run('center')
    demo.stop()
    check('stop 后状态为 stopping/stopped',
          demo.status()['status'] in ('stopping', 'stopped'),
          demo.status()['status'])
    wait_done(60)
    demo.stop()

    # 6) 并发保护：运行期间再次 run 应被拒
    demo.status()
    demo.RUN['status'] = 'running'
    r = demo.run('home')
    check('运行期间拒绝再次启动', r.get('ok') is False, r.get('error'))
    demo.RUN['status'] = 'idle'

    # 7) gpu-slicer 真调（允许失败，但必须给出原因而不是抛异常）
    r = demo._gpu_slicer(40.0)
    check('切分 Skill 真调且不抛异常', isinstance(r, dict)
          and ('result' in r or 'error' in r or 'raw' in r), str(r)[:100])

    print('\n========================================')
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
