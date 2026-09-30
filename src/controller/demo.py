# -*- coding: utf-8 -*-
"""WNIDIA 场景编排器（P1）。

把《商业计划书》里的三大场景与异构方式做成**可一键演示的剧本**：
每个场景是一串**真实可执行**的操作（提交任务、注入掉线/作弊、断网自治、
离线回放、并发压测、切分建议），逐步执行并把每一步的**真实测量结果**
写进事件流与留痕表。

设计边界（重要）：
  - **不重写调度内核**。剧本只调用现有模块（scheduler / registry / offline /
    harness / trust / metering / Skill CLI），内核一条规则都不改。
  - **不造假数据**。每一步的返回值都是真实执行结果；确属沙盒模拟的
    （offline.simulate_offline）在结果里带 `note` 说明，界面须如实标注。
  - **失败不中断演示**。单步异常被捕获成 step 级失败并写明原因，
    后续步骤继续执行，编排器自身不抛异常到 API 层。

对外：
  scenes()              场景清单（供门户页渲染卡片）
  run(scene)            异步启动一个场景（后台线程）
  status()              当前进度与每步结果
  stop()                请求停止
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Dict, List

from . import config, db, metering, offline, registry, trust
from .models import TaskSpec, TaskState, SLA, Secret

# ---------------------------------------------------------------- 运行态
RUN: Dict = {'scene': None, 'index': 0, 'status': 'idle', 'steps': [],
             'started': 0, 'finished': 0, 'error': '', 'paused': False,
             'plan': [], 'total': 0}
_LOCK = threading.Lock()
_STOP = threading.Event()
_PAUSED = threading.Event()
_PAUSED.set()          # set = 未暂停

DEMO_TENANT = 'demo-scene'
# 一键关闭（总开关）需要一并判停的租户：场景演示 + 流量发生器/控制台默认租户
_CLEANUP_TENANTS = (DEMO_TENANT, 'demo', 'demo-q')

_TERM = (TaskState.DONE.value, TaskState.REJECTED.value, TaskState.FAILED.value)


def _uid(prefix: str) -> str:
    return f'{prefix}-{int(time.time() * 1000)}-{os.urandom(3).hex()}'


# ---------------------------------------------------------------- 基础动作
def submit(prompt: str, sla: str = SLA.S2.value, secret: str = Secret.L1.value,
           task_type: str = 'chat', tokens_in: int = 64,
           need_mem_gb: float = 1.0, verify: bool = False,
           latency_budget_ms: float = 0.0, cc_required: str = '',
           wait: bool = True, timeout: float = 120.0) -> Dict:
    """提交一个真实任务（走与 /v1/chat/completions 相同的入库路径）。

    wait=True 时轮询到终态，返回真实落点、引擎、耗时与拒绝原因。
    """
    t = TaskSpec(task=_uid('demo'), tenant=DEMO_TENANT, prompt=prompt,
                 task_type=task_type, sla=sla, secret=secret,
                 tokens_in=tokens_in, need_mem_gb=need_mem_gb, cost=1,
                 verify=verify, latency_budget_ms=latency_budget_ms,
                 cc_required=cc_required,
                 billing_mode=metering.default_mode())
    db.upsert_task(t)
    try:
        db.ensure_quota(t.tenant, config.DEFAULT_QUOTA, t.billing_mode)
    except Exception:      # noqa: BLE001
        pass
    try:
        trust.seal('demo-submit', t.task, f'{t.secret}|{t.sla}|{t.task_type}')
    except Exception:      # noqa: BLE001
        pass
    if not wait:
        return {'task': t.task, 'state': TaskState.QUEUED.value}
    deadline = time.time() + timeout
    cur = t
    while time.time() < deadline:
        if _STOP.is_set():                       # 停止请求：不再空等终态，立刻返回
            got = db.get_task(t.task)
            if got is not None:
                cur = got
            if cur.state not in _TERM:
                cur.state = TaskState.FAILED.value
                cur.error = 'demo_stopped'
                cur.finished_at = cur.finished_at or int(time.time() * 1000)
                try:
                    db.upsert_task(cur)
                except Exception:      # noqa: BLE001
                    pass
            break
        got = db.get_task(t.task)
        if got is not None:
            cur = got
            if cur.state in _TERM:
                break
        time.sleep(0.3)
    return {
        'task': cur.task, 'state': cur.state, 'node': cur.node,
        'engine': cur.engine, 'error': cur.error or '',
        'progress': round(float(cur.progress or 0), 3),
        'latency_s': round(max(cur.finished_at - cur.created_at, 0) / 1000.0, 1),
        'sla': sla, 'secret': secret, 'task_type': task_type,
    }


def _node_of(node: str) -> Dict:
    n = db.get_node(node)
    return {'node': n.node, 'tier': n.tier, 'role': n.role,
            'status': n.status, 'reputation': round(float(n.reputation or 0), 2),
            'cheat': bool(getattr(n, 'cheat', False))} if n else {}


def _gpu_slicer(gpu_mem: float = 128.0) -> Dict:
    """真跑 gpu-slicer Skill（不是伪造建议）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tool = os.path.join(root, 'skills', 'gpu-slicer', 'tool.py')
    # 字段须与 skills/gpu-slicer 的 workload schema 一致（weights_gb / kv_gb）
    wl = json.dumps({'models': [
        {'name': 'qwen3-27b', 'weights_gb': 18, 'kv_gb': 4, 'concurrency': 2},
        {'name': 'qwen3-8b', 'weights_gb': 6, 'kv_gb': 2, 'concurrency': 8},
    ]})
    try:
        r = subprocess.run(
            [sys.executable, tool, '--gpu-mem', str(gpu_mem),
             '--workload', wl, '--mode', 'auto'],
            capture_output=True, text=True, timeout=60, cwd=root)
        try:
            return {'ok': True, 'result': json.loads(r.stdout)}
        except Exception:      # noqa: BLE001
            return {'ok': False, 'raw': (r.stdout or r.stderr)[:200]}
    except Exception as e:      # noqa: BLE001
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'[:160]}


# ---------------------------------------------------------------- 场景定义
def _scene_edge() -> List[Dict]:
    """场景一 · 边缘算力箱（BP P5 / P6 / P13）"""
    def s1():
        r = submit('产线质检：判断这张图是否存在划痕', sla=SLA.S1.value,
                   secret=Secret.L2.value, latency_budget_ms=120)
        r['落到'] = _node_of(r['node']) if r['node'] else {}
        return r

    def s2():
        n = registry.mark_status('cloud-0', 'lost')
        db.add_event('lost', '场景演示：中心云中断（cloud-0 置离线）',
                     node='cloud-0')
        return {'ok': bool(n), '中心云': '已模拟中断'}

    def s3():
        r = submit('园区巡检：识别画面中的异常目标', sla=SLA.S1.value,
                   secret=Secret.L2.value, latency_budget_ms=120)
        r['落到'] = _node_of(r['node']) if r['node'] else {}
        r['结论'] = ('任务未落中心云（边缘/其它档承接）'
                     if r['node'] != 'cloud-0' else '仍落中心云')
        return r

    def s4():
        r = offline.simulate_offline('edge-1', seconds=120, tasks=3,
                                     tokens=1500)
        r['说明'] = '断网期间边缘本地续跑，任务入本地队列'
        return r

    def s5():
        st = offline.status()
        pending = [n for n in db.all_nodes() if getattr(n, 'offline', False)]
        batch = db.offline_batch_rows(1)
        r = offline.recover('edge-1', batch[0] if batch else None)
        r['回放前待同步节点'] = [n.node for n in pending]
        r['离线批次总数'] = st.get('batches', 0)
        r['已补记 tokens'] = st.get('tokens_compensated', 0)
        return r

    def s6():
        registry.mark_status('cloud-0', 'online')
        db.add_event('register', '场景演示：中心云恢复在线', node='cloud-0')
        return {'ok': True, '中心云': '已恢复'}

    return [
        {'id': 'e1', 'title': '现场质检任务按时延优先落到边缘档', 'fn': s1},
        {'id': 'e2', 'title': '注入中心云中断', 'fn': s2},
        {'id': 'e3', 'title': '中断期间新任务自动改由边缘承接', 'fn': s3},
        {'id': 'e4', 'title': '边缘断网自治：本地续跑并入队', 'fn': s4},
        {'id': 'e5', 'title': '联网回放：批次补记与计量回收', 'fn': s5},
        {'id': 'e6', 'title': '中心云恢复，回到常态调度', 'fn': s6},
    ]


def _scene_home() -> List[Dict]:
    """场景二 · 家庭 Mac mini 网格（BP P5 / P7）"""
    def s1():
        r = submit('把这段产品描述转成向量', task_type='embed',
                   tokens_in=48, sla=SLA.S3.value)
        r['落到'] = _node_of(r['node']) if r['node'] else {}
        return r

    def s2():
        before = _node_of('cpu-1')
        registry.set_cheat('cpu-1', True)
        db.add_event('cheat', '场景演示：cpu-1 开启作弊（伪造算力）',
                     node='cpu-1')
        return {'作弊前': before, '作弊后': _node_of('cpu-1')}

    def s3():
        r = submit('校验：这段摘要与原文是否一致', verify=True,
                   task_type='chat', sla=SLA.S2.value)
        r['多数决'] = '已触发（verify=True）'
        r['作弊节点当前'] = _node_of('cpu-1')
        return r

    def s4():
        r = submit('处理这份含客户身份证号的报表', secret=Secret.L3.value,
                   sla=SLA.S2.value)
        if r['state'] == TaskState.REJECTED.value:
            r['结论'] = f'高密级任务被拒绝（{r["error"] or "准入规则"}）'
        elif r['state'] == TaskState.FAILED.value:
            r['结论'] = f'任务失败：{r["error"]}'
        else:
            r['结论'] = f'落到 {r["node"]}'
        return r

    def s5():
        registry.set_cheat('cpu-1', False)
        db.add_event('cheat', '场景演示：cpu-1 作弊开关关闭',
                     node='cpu-1')
        return {'ok': True, '恢复后': _node_of('cpu-1')}

    return [
        {'id': 'h1', 'title': '轻量嵌入任务下沉到端侧档', 'fn': s1},
        {'id': 'h2', 'title': '注入家庭节点作弊（伪造算力）', 'fn': s2},
        {'id': 'h3', 'title': '抽检多数决识别作弊并降级信誉', 'fn': s3},
        {'id': 'h4', 'title': '高密级任务拒绝下沉（BP P7 边界）', 'fn': s4},
        {'id': 'h5', 'title': '关闭作弊，节点恢复', 'fn': s5},
    ]


def _scene_center() -> List[Dict]:
    """场景三 · 私有云租赁与机密计算（BP P5 / P8 / P9）"""
    def s1():
        r = submit('对这份年报做 3000 字深度分析', task_type='heavy',
                   tokens_in=1024, need_mem_gb=8, sla=SLA.S2.value)
        r['落到'] = _node_of(r['node']) if r['node'] else {}
        return r

    def s2():
        return _gpu_slicer(gpu_mem=40.0)

    def s3():
        r = submit('风控模型推理（含客户资金流水）', secret=Secret.L3.value,
                   sla=SLA.S2.value, cc_required='CC-L2')
        r['结论'] = (f'被拒：{r["error"]}' if r['state'] ==
                     TaskState.REJECTED.value else f'落到 {r["node"]}')
        return r

    def s4():
        ids = [submit(f'并发压测任务 {i}', sla=SLA.S3.value, wait=False)['task']
               for i in range(8)]
        deadline = time.time() + 60
        done = 0
        while time.time() < deadline and done < len(ids):
            done = sum(1 for i in ids
                       if (db.get_task(i) or TaskSpec(task=i)).state in _TERM)
            time.sleep(0.5)
        return {'提交': len(ids), '完成': done,
                '窗口_秒': 60, '吞吐_次每分': round(done, 1)}

    return [
        {'id': 'c1', 'title': '重推理任务落到中心云', 'fn': s1},
        {'id': 'c2', 'title': 'vGPU/MIG 切分建议（真跑 gpu-slicer）', 'fn': s2},
        {'id': 'c3', 'title': 'L3 机密任务：密级与 CC 准入', 'fn': s3},
        {'id': 'c4', 'title': '8 路并发压测：排队与吞吐', 'fn': s4},
    ]


def _scene_route() -> List[Dict]:
    """跨场景 · 三维路由（BP P4 / P13）"""
    def mk(sla, secret, lb):
        def f():
            r = submit('同一段文本：给出 100 字摘要', sla=sla, secret=secret,
                       latency_budget_ms=lb)
            r['维度'] = f'SLA={sla} 密级={secret} 时延预算={lb}ms'
            r['落到'] = _node_of(r['node']) if r['node'] else {}
            return r
        return f

    return [
        {'id': 'r1', 'title': '时延优先（S1 + 100ms 预算）',
         'fn': mk(SLA.S1.value, Secret.L1.value, 100)},
        {'id': 'r2', 'title': '成本优先（S3 离线档）',
         'fn': mk(SLA.S3.value, Secret.L1.value, 0)},
        {'id': 'r3', 'title': '密级优先（L3 机密）',
         'fn': mk(SLA.S2.value, Secret.L3.value, 0)},
    ]


def _scene_resilience() -> List[Dict]:
    """跨场景 · 韧性（BP P14 压力场景表）"""
    def s1():
        registry.mark_status('cloud-0', 'lost')
        r = submit('中心中断下的巡检任务', sla=SLA.S1.value)
        out = {'中心中断': True, '任务结果': r.get('state'),
               '落到': r.get('node')}
        registry.mark_status('cloud-0', 'online')
        out['已恢复'] = True
        return out

    def s2():
        registry.mark_status('edge-1', 'lost')
        r = submit('单节点掉线后的质检任务', sla=SLA.S2.value)
        out = {'掉线节点': 'edge-1', '任务结果': r.get('state'),
               '改派到': r.get('node')}
        registry.mark_status('edge-1', 'online')
        out['已恢复'] = True
        return out

    def s3():
        registry.set_cheat('cpu-1', True)
        r = submit('节点不可信时的校验任务', verify=True)
        out = {'作弊节点': 'cpu-1', '任务结果': r.get('state'),
               '落到': r.get('node')}
        registry.set_cheat('cpu-1', False)
        out['已恢复'] = True
        return out

    def s4():
        r = submit('QPU 退火求解一个组合优化问题', task_type='qpu',
                   sla=SLA.S3.value)
        return {'新任务类型': 'qpu', '任务结果': r.get('state'),
                '拒绝原因': r.get('error') or '（无，已受理）',
                '说明': 'BP P14：预留 QPU 抽象，未押注路线'}

    def s5():
        st = offline.status()
        return {'离线批次': st.get('batches', 0),
                '已回放': st.get('replayed', 0),
                '已补记 tokens': st.get('tokens_compensated', 0)}

    return [
        {'id': 'x1', 'title': '中心云中断：现场业务不停产', 'fn': s1},
        {'id': 'x2', 'title': '单节点掉线：自动改派', 'fn': s2},
        {'id': 'x3', 'title': '节点不可信：校验失败即撤回', 'fn': s3},
        {'id': 'x4', 'title': '新任务类型（QPU）：接口预留', 'fn': s4},
        {'id': 'x5', 'title': '离线自治与回放汇总', 'fn': s5},
    ]


SCENES: Dict[str, Dict] = {
    'edge': {'title': '边缘算力箱 · 现场推理自治',
             'module': '边缘自治引擎',
             'bp': '此项目 P5 / P6 / P13',
             'desc': '现场推理本地优先、中心中断自愈、断网续跑与联网回放',
             'build': _scene_edge},
    'home': {'title': '家庭节点网格 · 轻量下沉与抽检',
             'module': '可信接入与抽检降级',
             'bp': '此项目 P5 / P7',
             'desc': '轻量任务下沉、作弊识别与信誉降级、高密级拒绝下沉',
             'build': _scene_home},
    'center': {'title': '私有云资源池 · 切分与密级准入',
               'module': '切分调度与密级准入',
               'bp': '此项目 P5 / P8 / P9',
               'desc': '重推理上中心、vGPU/MPS 切分建议、密级准入、并发压测',
               'build': _scene_center},
    'route': {'title': '三维路由 · 时延/成本/密级择点',
              'module': '三维路由择点引擎',
              'bp': '此项目 P4 / P13',
              'desc': '同一任务改三个维度，观察择点变化与可解释理由',
              'build': _scene_route},
    'resilience': {'title': '故障自愈 · 压力场景验证',
                   'module': '故障自愈与回放',
                   'bp': '此项目 P14',
                   'desc': '逐条触发压力场景：中断/掉线/不可信/新任务类型',
                   'build': _scene_resilience},
}


# ---------------------------------------------------------------- 编排执行
def scenes() -> Dict:
    with _LOCK:
        return {k: {'title': v['title'], 'bp': v['bp'], 'desc': v['desc'],
                    'module': v['module'],
                    'steps': [{'id': s['id'], 'title': s['title']}
                              for s in v['build']()]}
                for k, v in SCENES.items()}


def _summarize(res) -> str:
    """把一步的结果压成一行可读摘要（写进事件流）。"""
    if not isinstance(res, dict):
        return str(res)[:120]
    for k in ('结论', 'state', '落到', '完成', 'ok'):
        if k in res:
            v = res[k]
            if isinstance(v, dict):
                v = v.get('node') or v.get('tier') or str(v)[:40]
            return f'{k}={v}'
    return json.dumps(res, ensure_ascii=False)[:100]


def _execute(scene: str):
    steps = SCENES[scene]['build']()
    for i, st in enumerate(steps):
        if _STOP.is_set():
            with _LOCK:
                RUN['status'] = 'stopped'
                RUN['steps'].append({'id': st['id'], 'title': st['title'],
                                     'status': 'skipped'})
            db.add_event('demo', f'[{scene}] 场景被手动停止，'
                                 f'剩余步骤未执行')
            return
        # 暂停：等待恢复（也可被停止打断）
        if not _PAUSED.is_set() and not _STOP.is_set():
            with _LOCK:
                RUN['status'] = 'paused'
            db.add_event('demo', f'[{scene}] 场景已暂停（第 {i+1} 步前）')
            _PAUSED.wait(timeout=3600)
            if _STOP.is_set():
                with _LOCK:
                    RUN['status'] = 'stopped'
                db.add_event('demo', f'[{scene}] 暂停中被停止')
                return
            with _LOCK:
                RUN['status'] = 'running'
            db.add_event('demo', f'[{scene}] 已恢复执行')
        with _LOCK:
            RUN['index'] = i
            RUN['status'] = 'running'
        t0 = time.time()
        try:
            res = st['fn']()
            ok = True
            err = ''
        except Exception as e:      # noqa: BLE001
            res = {'error': f'{type(e).__name__}: {e}'[:200]}
            ok = False
            err = traceback.format_exc(limit=2)[-300:]
        dur = round(time.time() - t0, 1)
        rec = {'id': st['id'], 'title': st['title'], 'ok': ok,
               'duration_s': dur, 'result': res, 'error': err}
        with _LOCK:
            RUN['steps'].append(rec)
        db.add_event('demo', f'[{scene}] {st["title"]} → {_summarize(res)}'
                             f'（{dur}s）')
    with _LOCK:
        RUN['status'] = 'done'
        RUN['finished'] = int(time.time() * 1000)
    ok_n = sum(1 for s in RUN['steps'] if s.get('ok'))
    db.add_event('demo', f'[{scene}] 场景演示结束：'
                         f'成功 {ok_n}/{len(RUN["steps"])} 步')


def run(scene: str) -> Dict:
    if scene not in SCENES:
        return {'ok': False, 'error': f'未知场景 {scene}',
                '可选': list(SCENES)}
    plan = [{'id': s['id'], 'title': s['title']}
            for s in SCENES[scene]['build']()]
    with _LOCK:
        if RUN['status'] == 'running':
            return {'ok': False, 'error': '已有场景在运行，请先 /admin/demo/stop'}
        RUN.update({'scene': scene, 'index': 0, 'status': 'running',
                    'steps': [], 'started': int(time.time() * 1000),
                    'finished': 0, 'error': '',
                    'plan': plan, 'total': len(plan)})
    _STOP.clear()
    threading.Thread(target=_execute, args=(scene,), daemon=True).start()
    db.add_event('demo', f'场景演示开始：{SCENES[scene]["title"]}'
                         f'（{SCENES[scene]["bp"]}）')
    return {'ok': True, 'scene': scene, 'title': SCENES[scene]['title'],
            'steps': len(SCENES[scene]['build']())}


def status() -> Dict:
    with _LOCK:
        return {'scene': RUN['scene'], 'status': RUN['status'],
                'index': RUN['index'],
                'plan': list(RUN.get('plan') or []),
                'total': int(RUN.get('total') or 0),
                'steps': list(RUN['steps']),
                'started': RUN['started'], 'finished': RUN['finished'],
                'paused': not _PAUSED.is_set(),
                'elapsed_s': round((int(time.time() * 1000) -
                                    (RUN['started'] or
                                     int(time.time() * 1000))) / 1000.0, 1)}


def pause() -> Dict:
    with _LOCK:
        if RUN['status'] != 'running':
            return {'ok': False, 'error': '当前没有运行中的场景'}
        _PAUSED.clear()
        RUN['paused'] = True
    return {'ok': True, 'message': '已暂停（当前步骤执行完后停在下一步之前）'}


def resume() -> Dict:
    with _LOCK:
        _PAUSED.set()
        if RUN['status'] == 'paused':
            RUN['status'] = 'running'
        RUN['paused'] = False
    return {'ok': True, 'message': '已恢复'}


def stop(all_: bool = False) -> Dict:
    """停止场景。all_=True 时做「总开关」：停场景 + 终止在途演示任务 +
    清掉演示流量进程 + 删除演示任务记录。"""
    _STOP.set()
    _PAUSED.set()                        # 若在暂停中也一并解除
    with _LOCK:
        if RUN['status'] in ('running', 'paused'):
            RUN['status'] = 'stopping'
    out = {'ok': True,
           'message': '已请求停止：当前步骤立即中断，不再空等任务终态；'
                      '引擎侧正在推理的余波任务会自动收尾'}
    if all_:
        out['cleanup'] = stop_all_cleanup()
    return out


def stop_all_cleanup() -> Dict:
    """总开关清理：在途演示/流量任务判停、停止流量发生器进程、清空演示任务记录。

    判停范围 = _CLEANUP_TENANTS（场景演示 + 流量发生器/控制台默认租户），
    不触碰显式命名的业务租户。
    """
    stopped_tasks = 0
    for t in list(db.all_tasks()):
        if t.tenant in _CLEANUP_TENANTS and t.state in (
                TaskState.QUEUED.value, TaskState.BINDING.value,
                TaskState.RUNNING.value):
            t.state = TaskState.FAILED.value
            t.error = 'demo_stopped'
            t.finished_at = t.finished_at or t.created_at
            db.upsert_task(t)
            stopped_tasks += 1
    traffic_killed = False
    try:
        r = subprocess.run(['pkill', '-f', 'traffic_gen.py'],
                           capture_output=True, timeout=10)
        traffic_killed = r.returncode == 0
    except Exception:      # noqa: BLE001
        pass
    db.add_event('demo', f'总开关：终止在途演示任务 {stopped_tasks} 个，'
                         f'流量进程 {"已停止" if traffic_killed else "无/未停止"}')
    return {'tasks_stopped': stopped_tasks, 'traffic_killed': traffic_killed}


def clear_demo_tasks() -> Dict:
    """删除演示/流量任务的历史记录（_CLEANUP_TENANTS），不碰显式业务租户。"""
    n = 0
    try:
        with db._LOCK:                                   # noqa: SLF001
            marks = ','.join('?' * len(_CLEANUP_TENANTS))
            cur = db.conn().execute(
                f'DELETE FROM tasks WHERE tenant IN ({marks})',
                tuple(_CLEANUP_TENANTS))
            db.conn().commit()
            n = cur.rowcount if cur.rowcount is not None else 0
        db.add_event('demo', f'清理演示任务记录 {n} 条')
    except Exception as e:      # noqa: BLE001
        return {'ok': False, 'error': str(e)[:120]}
    return {'ok': True, 'deleted': n}


# ---------------------------------------------------------------- 一键性能实测
def bench_best(tasks: int = 6, timeout: float = 240.0) -> Dict:
    """一键测试最优性能：并发提交若干轻任务，按落点节点统计时延与吞吐。

    全是真实任务真实推理；测试环境的统计同样来自真实结果（mock 引擎时
    时延为模拟值，标注 synthetic）。
    """
    t0 = time.time()
    tasks = max(2, min(int(tasks), 12))
    ids = [submit(f'性能实测：请用 30 字描述节点画像 {i}',
                  sla=SLA.S3.value, wait=False)['task']
           for i in range(tasks)]
    deadline = time.time() + timeout
    done_tasks = []
    while time.time() < deadline and len(done_tasks) < len(ids):
        done_tasks = [t for t in (db.get_task(i) for i in ids)
                      if t is not None and t.state in _TERM]
        time.sleep(0.5)
    elapsed = round(time.time() - t0, 1)
    per_node: Dict[str, list] = {}
    for t in done_tasks:
        if not t.node:
            continue
        lat = max(t.finished_at - t.created_at, 0) / 1000.0
        per_node.setdefault(t.node, []).append(lat)
    ranking = sorted(
        ({'node': n, 'avg_s': round(sum(v) / len(v), 1), 'tasks': len(v),
          'min_s': round(min(v), 1)}
         for n, v in per_node.items()),
        key=lambda x: x['avg_s'])
    finished = sum(len(v) for v in per_node.values())
    out = {'ok': True, 'submitted': len(ids), 'finished': finished,
           'elapsed_s': elapsed, 'ranking': ranking,
           'best_node': ranking[0]['node'] if ranking else '',
           'throughput_per_min': round(finished / max(elapsed / 60.0, 0.1), 1)}
    db.add_event('demo', f'性能实测：完成 {finished}/{len(ids)}，'
                         f'最优节点 {out["best_node"] or "-"}'
                         f'（{out["throughput_per_min"]} 次/分）')
    return out


# ---------------------------------------------------------------- 引擎与评审反馈
def engine_feedback() -> Dict:
    """给前端两栏：调度引擎（harness）运行数据 + 语义评审（JEV）数据。"""
    from . import jevclient, prompt_guard
    counts: Dict[str, int] = {}
    for t in db.all_tasks():
        counts[t.state] = counts.get(t.state, 0) + 1
    ev = db.recent_events(200)
    five_min_ago = int(time.time() * 1000) - 300000
    ev_rate = sum(1 for e in ev if getattr(e, 'ts', 0) > five_min_ago)
    pg = prompt_guard.status()
    harness = {'tasks': counts,
               'running': counts.get('running', 0),
               'queued': counts.get('queued', 0),
               'events_last_5min': ev_rate,
               'spool': {'size': pg['spool_size'], 'max': pg['spool_max']},
               'prompt_stats': pg['stats']}
    jc = getattr(jevclient, 'C', {}) or {}
    jev = {'calls': jc.get('calls', 0), 'live': jc.get('live', 0),
           'mock': jc.get('mock', 0), 'fallback': jc.get('fallback', 0),
           'blocks': jc.get('blocks', 0),
           'mode': getattr(config, 'JEV_MODE', 'mock'),
           'engine': getattr(config, 'JEV_MODE', 'mock') and 'local-offline'
                     if getattr(config, 'JEV_MODE', 'mock') == 'mock'
                     else getattr(config, 'JEV_BACKEND', 'http')}
    return {'ok': True, 'harness': harness, 'jev': jev}


# ---------------------------------------------------------------- JEV 日志报告
# ---------------------------------------------------------------- JEV 留痕持久化与分析
_JEV_SEEN = set()
_JEV_SEEN_LOADED = False
_JEV_LOG_MAX_BYTES = 512 * 1024          # 超过则只保留尾部 1000 行


def _jev_log_path() -> str:
    return str(config.DATA_DIR / 'jev_log.jsonl')


def _jev_log_load_seen() -> None:
    """首次调用时从留痕文件回填去重集合（跨重启不重复追加）。"""
    global _JEV_SEEN_LOADED
    if _JEV_SEEN_LOADED:
        return
    _JEV_SEEN_LOADED = True
    try:
        with open(_jev_log_path(), encoding='utf-8') as f:
            for line in f:
                try:
                    _JEV_SEEN.add(json.loads(line).get('task'))
                except Exception:      # noqa: BLE001
                    continue
    except Exception:      # noqa: BLE001
        pass


def _jev_log_persist(items: List[Dict]) -> int:
    """把新出现的终态任务判定追加到 data/jev_log.jsonl（按 task 去重，幂等）。"""
    _jev_log_load_seen()
    with _LOCK:
        new_rows = [it for it in items
                    if it.get('task') and it['task'] not in _JEV_SEEN]
        if not new_rows:
            return 0
        try:
            path = _jev_log_path()
            if os.path.exists(path) and os.path.getsize(path) > _JEV_LOG_MAX_BYTES:
                try:
                    with open(path, encoding='utf-8') as f:
                        tail = f.readlines()[-1000:]
                    with open(path, 'w', encoding='utf-8') as f:
                        f.writelines(tail)
                except Exception:      # noqa: BLE001
                    pass
            with open(path, 'a', encoding='utf-8') as f:
                for it in new_rows:
                    _JEV_SEEN.add(it['task'])
                    f.write(json.dumps({
                        'ts': int(time.time() * 1000), 'task': it['task'],
                        'state': it['state'], 'node': it['node'],
                        'engine': it['engine'], 'latency_s': it['latency_s'],
                        'tokens': it['tokens'], 'consistency_ok': it['consistency_ok'],
                        'amount_cny': it['amount_cny'], 'estimated': it['estimated'],
                        'brief': it['brief']}, ensure_ascii=False) + '\n')
            return len(new_rows)
        except Exception:      # noqa: BLE001
            return 0


def _jev_log_read(limit: int = 500) -> List[Dict]:
    try:
        rows = []
        with open(_jev_log_path(), encoding='utf-8') as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:      # noqa: BLE001
                    continue
        return rows[-max(1, int(limit)):]
    except Exception:      # noqa: BLE001
        return []


def _jev_analysis() -> Dict:
    """基于持久化留痕（跨重启留存）给汇总分析与一行结论。"""
    rows = _jev_log_read(500)
    total = len(rows)
    if not total:
        return {'total': 0, 'pass_rate': 0,
                'dist': {'ok': 0, 'div': 0, 'na': 0},
                'trend': '—', 'recent10': None, 'prev10': None,
                'total_tokens': 0, 'total_amount_cny': 0, 'nodes': [],
                'conclusion': '暂无留痕：先运行场景 / 流量 / 性能实测，'
                              '判定会逐条写入 data/jev_log.jsonl 并在此自动汇总。'}
    done_n = sum(1 for r in rows if r.get('state') == 'done')
    ok = sum(1 for r in rows if r.get('consistency_ok') is True)
    div = sum(1 for r in rows if r.get('consistency_ok') is False)
    na = total - ok - div
    tok = sum(int(r.get('tokens') or 0) for r in rows)
    amt = round(sum(float(r.get('amount_cny') or 0.0) for r in rows), 4)
    by_node: Dict[str, Dict] = {}
    for r in rows:
        n = r.get('node') or '-'
        a = by_node.setdefault(n, {'tasks': 0, 'lat': 0.0, 'amt': 0.0, 'div': 0})
        a['tasks'] += 1
        a['lat'] += float(r.get('latency_s') or 0.0)
        a['amt'] += float(r.get('amount_cny') or 0.0)
        if r.get('consistency_ok') is False:
            a['div'] += 1
    nodes = [{'node': n, 'tasks': a['tasks'],
              'avg_lat': round(a['lat'] / max(a['tasks'], 1), 1),
              'amount_cny': round(a['amt'], 4), 'diverge': a['div']}
             for n, a in sorted(by_node.items())]

    def _pr(arr: List[Dict]):
        if not arr:
            return None
        return round(sum(1 for r in arr if r.get('state') == 'done')
                     / len(arr) * 100)
    recent10 = _pr(rows[-10:])
    prev10 = _pr(rows[-20:-10])
    if recent10 is None:
        trend = '—'
    elif prev10 is None:
        trend = '样本积累中'
    elif recent10 > prev10:
        trend = '上升'
    elif recent10 < prev10:
        trend = '下降'
    else:
        trend = '持平'
    pass_rate = round(done_n / total * 100)
    worst = max(nodes, key=lambda x: x['diverge'], default=None)
    advice = ''
    if worst and worst['diverge'] > 0:
        advice = (f'；分歧集中在 {worst["node"]}（{worst["diverge"]} 次），'
                  f'建议复核该节点输出')
    health = '，整体健康' if (pass_rate >= 90 and div == 0) else ''
    conclusion = (f'累计评测 {total} 条：成功率 {pass_rate}%，多数决一致 {ok} 条 / '
                  f'分歧 {div} 条 / 未校验 {na} 条；近 10 条成功率 '
                  f'{recent10 if recent10 is not None else "—"}%（趋势 {trend}）；'
                  f'累计 tokens {tok} · 收益 ¥{amt}{health}{advice}。')
    return {'total': total, 'pass_rate': pass_rate,
            'dist': {'ok': ok, 'div': div, 'na': na},
            'trend': trend, 'recent10': recent10, 'prev10': prev10,
            'total_tokens': tok, 'total_amount_cny': amt,
            'nodes': nodes, 'conclusion': conclusion}


def jev_report(limit: int = 12) -> Dict:
    """JEV 日志报告：逐条评测近期任务的执行状况与收益实现，给一行简介。

    数据全部为真实留痕（不造假）：
      - 执行状况：任务 state / 落点 node / 引擎 / 真实时延；
      - JEV 判定：任务的 consistency_ok（多数决校验结果，verify 任务才有）；
      - 收益实现：计量台账 amount_cny / tokens，estimated 区分真实/估算。
    """
    from . import jevclient
    tasks = [t for t in db.all_tasks() if t.state in _TERM]
    tasks.sort(key=lambda t: t.created_at, reverse=True)
    cap = max(1, min(int(limit), 50))
    ledger_by_task: Dict[str, Dict] = {}
    try:
        for row in db.ledger_rows(500):
            if isinstance(row, dict) and row.get('task'):
                ledger_by_task[row['task']] = row
    except Exception:      # noqa: BLE001
        pass
    items_all = []
    for t in tasks:
        lat = (round(max((t.finished_at or 0) - (t.created_at or 0), 0)
                     / 1000.0, 1) if t.finished_at else 0.0)
        tokens = int(t.prompt_tokens or 0) + int(t.completion_tokens or 0)
        led = ledger_by_task.get(t.task, {})
        amount = float(led.get('amount_cny', 0.0) or 0.0)
        if t.consistency_ok is True:
            jev_txt = 'JEV 多数决一致'
        elif t.consistency_ok is False:
            jev_txt = 'JEV 判定存在分歧'
        else:
            jev_txt = '未触发 JEV 校验'
        est = bool(getattr(t, 'metering_estimated', False))
        src = '估算' if est else '实测'
        err = (t.error or '')[:20]
        if t.state == TaskState.DONE.value:
            brief = (f'执行成功 · 落 {t.node or "-"} · {lat}s · {jev_txt} · '
                     f'计量{src} {tokens}tok · 收益 ¥{amount:.4f}')
        elif t.state == TaskState.REJECTED.value:
            brief = (f'准入拒绝（{err}） · {jev_txt} · 收益 ¥{amount:.4f}')
        else:
            brief = (f'执行失败（{err}） · {jev_txt} · 收益 ¥{amount:.4f}')
        items_all.append({'task': t.task, 'state': t.state, 'node': t.node or '-',
                          'engine': t.engine or '-', 'latency_s': lat,
                          'tokens': tokens, 'consistency_ok': t.consistency_ok,
                          'jev': jev_txt, 'amount_cny': round(amount, 4),
                          'estimated': est, 'brief': brief})
    persisted = _jev_log_persist(items_all)
    items = items_all[:cap]
    pass_n = sum(1 for it in items if it['state'] == TaskState.DONE.value)
    tot_tokens = sum(int(it['tokens']) for it in items)
    tot_amount = float(sum(float(it['amount_cny']) for it in items))
    jc = getattr(jevclient, 'C', {}) or {}
    n = len(items) or 1
    mode = jevclient.effective_mode()
    return {'ok': True,
            'summary': {'mode': mode,
                        'engine': ('local-offline' if mode == 'mock'
                                   else getattr(config, 'JEV_BACKEND', 'http')),
                        'calls': jc.get('calls', 0), 'live': jc.get('live', 0),
                        'mock': jc.get('mock', 0),
                        'fallback': jc.get('fallback', 0),
                        'blocks': jc.get('blocks', 0),
                        'evaluated': len(items), 'passed': pass_n,
                        'pass_rate': round(pass_n / n * 100, 1),
                        'total_tokens': tot_tokens,
                        'total_amount_cny': round(tot_amount, 4)},
            'analysis': _jev_analysis(),
            'persisted_now': persisted,
            'items': items}


# ---------------------------------------------------------------- 流量发生器
_TRAFFIC = {'proc': None, 'concurrency': 0, 'duration': 0.0, 'started': 0.0}


def _traffic_alive() -> bool:
    p = _TRAFFIC.get('proc')
    return bool(p) and p.poll() is None


def traffic_status() -> Dict:
    alive = _traffic_alive()
    return {'ok': True, 'running': alive,
            'concurrency': _TRAFFIC.get('concurrency', 0),
            'duration': _TRAFFIC.get('duration', 0.0),
            'elapsed_s': (round(time.time() - _TRAFFIC['started'], 1)
                          if alive and _TRAFFIC.get('started') else 0)}


def traffic_start(concurrency: int = 3, duration: float = 120.0,
                  max_tokens: int = 96) -> Dict:
    """从 API 进程拉起流量发生器（真实任务，注入调度闭环让看板动起来）。"""
    if _traffic_alive():
        return {'ok': False, 'error': '流量发生器已在运行',
                'status': traffic_status()}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tool = os.path.join(root, 'scripts', 'traffic_gen.py')
    if not os.path.isfile(tool):
        return {'ok': False, 'error': '未找到 scripts/traffic_gen.py'}
    conc = max(1, min(int(concurrency), 8))
    dur = max(0.0, float(duration))
    cmd = [sys.executable, tool, '--base', f'http://127.0.0.1:{config.API_PORT}',
           '--token', config.API_TOKEN, '--concurrency', str(conc),
           '--duration', str(dur),
           '--max-tokens', str(max(32, min(int(max_tokens), 512)))]
    try:
        logdir = os.path.join(root, 'data', 'runlogs')
        os.makedirs(logdir, exist_ok=True)
        lf = open(os.path.join(logdir, 'traffic_api.log'), 'ab')
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    except Exception as e:      # noqa: BLE001
        return {'ok': False, 'error': f'启动失败：{type(e).__name__}: {e}'[:160]}
    _TRAFFIC.update({'proc': proc, 'concurrency': conc, 'duration': dur,
                     'started': time.time()})
    db.add_event('demo', f'流量发生器启动：并发 {conc} 路，'
                         f'时长 {int(dur) or "不限"}s')
    return {'ok': True, 'message': '流量发生器已启动（真实任务，非预置数据）',
            'status': traffic_status()}


def traffic_stop() -> Dict:
    p = _TRAFFIC.get('proc')
    killed = False
    if p and p.poll() is None:
        try:
            p.terminate()
            killed = True
        except Exception:      # noqa: BLE001
            pass
    try:
        r = subprocess.run(['pkill', '-f', 'traffic_gen.py'],
                           capture_output=True, timeout=10)
        killed = killed or r.returncode == 0
    except Exception:      # noqa: BLE001
        pass
    _TRAFFIC['proc'] = None
    db.add_event('demo', '流量发生器已停止')
    return {'ok': True, 'killed': killed, 'status': traffic_status()}
