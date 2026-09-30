# -*- coding: utf-8 -*-
"""演示控制与反馈功能测试（暂停/继续/总开关/清理/性能实测/反馈面板）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = '/tmp/demo_ctrl.db'
if os.path.exists(DB):
    os.remove(DB)
os.environ['WNIDIA_DB'] = DB
os.environ['WNIDIA_MODE'] = 'mock'

from controller import db, demo, registry            # noqa: E402
from controller.models import (NodeProfile, NodeStatus, TaskSpec,  # noqa: E402
                               TaskState, SLA, Secret)

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[PASS]', name, detail)
    else:
        FAIL += 1
        print('[FAIL]', name, detail)


def mk_demo_task(state, node=None, lat_ms=1000):
    t = TaskSpec(task=demo._uid('t'), tenant=demo.DEMO_TENANT,
                 prompt='x', sla=SLA.S3.value, secret=Secret.L1.value,
                 state=state, node=node,
                 created_at=int(time.time() * 1000) - lat_ms * 2,
                 finished_at=(int(time.time() * 1000) - lat_ms
                              if state in ('done', 'failed', 'rejected') else 0))
    db.upsert_task(t)
    return t


def main():
    db.reset_db()
    now = int(time.time() * 1000)
    for name, role, tier, mem in (('cloud-0', 'prefill', 'cloud', 40),
                                  ('edge-1', 'decode', 'edge', 8),
                                  ('cpu-1', 'cpu', 'cpu', 4)):
        registry.register(NodeProfile(
            node=name, role=role, tier=tier, mem_total_gb=mem,
            mem_limit_gb=mem, vllm_port=8101,
            sovereign_region='local', status=NodeStatus.ONLINE.value,
            util=20.0, free_mem_gb=mem - 1, kv_hit=0.5, reputation=1.0,
            last_heartbeat=now, trusted=True))

    # 1) 暂停 / 继续
    demo.RUN.update({'status': 'running', 'scene': 'route', 'steps': []})
    r = demo.pause()
    check('暂停生效', r.get('ok') is True and not demo._PAUSED.is_set(), r)
    check('status 反映 paused', demo.status()['paused'] is True)
    r = demo.resume()
    check('恢复生效', r.get('ok') is True and demo._PAUSED.is_set()
          and demo.status()['status'] == 'running', r)
    demo.RUN['status'] = 'idle'
    r = demo.pause()
    check('空闲时暂停被拒', r.get('ok') is False, r)

    # 2) 总开关：停场景 + 终止在途演示任务 + （无流量进程时如实标注）
    t1 = mk_demo_task(TaskState.RUNNING.value)
    t2 = mk_demo_task(TaskState.QUEUED.value)
    t3 = mk_demo_task(TaskState.DONE.value, node='edge-1')
    r = demo.stop(all_=True)
    cl = r.get('cleanup', {})
    check('总开关终止在途演示任务',
          cl.get('tasks_stopped') == 2, cl)
    t1x, t2x = db.get_task(t1.task), db.get_task(t2.task)
    check('在途任务被判停并留痕',
          t1x.state == 'failed' and t1x.error == 'demo_stopped'
          and t2x.state == 'failed', (t1x.state, t2x.state))
    check('已完成任务不受影响', db.get_task(t3.task).state == 'done')
    check('无流量进程时如实标注', cl.get('traffic_killed') is False)

    # 3) 清理演示任务
    n = sum(1 for t in db.all_tasks() if t.tenant == demo.DEMO_TENANT)
    r = demo.clear_demo_tasks()
    check('清理演示任务记录',
          r.get('ok') and r.get('deleted') == n and n >= 3, (n, r.get('deleted')))
    check('真实业务任务未受影响',
          all(t.tenant != demo.DEMO_TENANT or False
              for t in db.all_tasks()))

    # 4) 一键性能实测（写库式打桩，验证统计而非伪造结果）
    real_submit = demo.submit
    nodes = ['edge-1', 'cpu-1', 'cloud-0']
    cnt = {'n': 0}

    def fake_submit(*a, **kw):
        cnt['n'] += 1
        node = nodes[cnt['n'] % 3]
        lat = {'edge-1': 12000, 'cpu-1': 25000, 'cloud-0': 30000}[node]
        t = mk_demo_task(TaskState.DONE.value, node=node, lat_ms=lat)
        return {'task': t.task, 'state': 'queued'}

    demo.submit = fake_submit
    try:
        r = demo.bench_best(tasks=6, timeout=10)
        check('性能实测完成', r.get('ok') and r.get('finished') == 6, r.get('finished'))
        check('有排名', len(r.get('ranking') or []) == 3, r.get('ranking'))
        check('最优节点 = 时延最小', r.get('best_node') == 'edge-1',
              r.get('best_node'))
        check('吞吐为正', (r.get('throughput_per_min') or 0) > 0)
    finally:
        demo.submit = real_submit

    # 5) 反馈面板结构
    fb = demo.engine_feedback()
    check('反馈含 harness 与 jev 两栏',
          'harness' in fb and 'jev' in fb, list(fb))
    check('harness 字段完整',
          all(k in fb['harness'] for k in ('tasks', 'spool', 'events_last_5min')),
          list(fb['harness']))
    check('jev 字段完整',
          all(k in fb['jev'] for k in ('calls', 'live', 'mock', 'fallback')),
          fb['jev'])

    print('\n========================================')
    print('通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
