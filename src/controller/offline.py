# -*- coding: utf-8 -*-
"""边缘断网自治（BP P13 决策③ / P14 韧性第一条）。

能力边界（如实标注）：
    - 本模块实现的是**控制面侧**：断网判定、批次回放、计量补偿、策略同步回执。
    - 边缘侧的"本地队列 + 断网续跑"在 `worker/agent.py` 中实现。
    - 沙盒可用 `simulate_offline()` 伪造一次断网-恢复，无需真的拔网线。

设计借鉴（非拷贝）：
- **KubeEdge**（CNCF，~7k★）的边缘自治：MetaManager 在边缘侧缓存元数据，
  断网期间应用照常运行，联网后与云端增量同步。
- **OpenYurt**（CNCF）的 YurtHub：节点上的本地配置中心，断网期间持续为
  本地业务提供配置服务；以及 **无侵入** 的设计取向。
- **EdgeFlow** 的重连退避与模型分发：本项目在 worker 侧沿用「指数退避重连」。
本项目在此基础上把"断网期间的算力"做成**可计量的补偿批次**，
这是 BP P14「联网后自动同步计量与策略」的落点。
"""
import hashlib
import time

from . import db, metering, trust


def _now():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 状态
def mark_offline(node_name, since=None):
    n = db.get_node(node_name)
    if not n:
        return None
    n.offline = True
    n.offline_since = int(since or _now())
    db.upsert_node(n)
    db.add_event('offline', f'{node_name} 进入断网自治（本地队列接管）',
                 node=node_name)
    trust.seal('offline-start', node_name, str(n.offline_since))
    return n


def mark_online(node_name):
    n = db.get_node(node_name)
    if not n:
        return None
    was = bool(n.offline)
    n.offline = False
    n.offline_since = 0
    n.pending_local = 0
    db.upsert_node(n)
    if was:
        db.add_event('online', f'{node_name} 恢复联网，等待批次回放',
                     node=node_name)
    return n


def set_pending(node_name, count):
    n = db.get_node(node_name)
    if not n:
        return None
    n.pending_local = max(0, int(count))
    db.upsert_node(n)
    return n


# ---------------------------------------------------------------- 批次回放
def build_batch(node_name, tasks, tokens, node_seconds, note=''):
    """构造一个批次（worker 或沙盒调用）。"""
    batch_id = 'ob-' + hashlib.sha1(
        f'{node_name}|{_now()}|{tasks}'.encode('utf-8')).hexdigest()[:12]
    return {'batch_id': batch_id, 'node': node_name, 'tasks': int(tasks),
            'tokens': int(tokens), 'node_seconds': float(node_seconds),
            'note': note}


def ingest_batch(batch, tenant='demo', engine='local', replay=True):
    """控制面接收并回放一个离线批次：落库 + 计量补偿 + 审计留痕。

    **幂等**：同一 batch_id 重复提交只记一次（防断网重连时的重复上报）。
    """
    if not isinstance(batch, dict) or not batch.get('batch_id'):
        return {'ok': False, 'reason': '批次缺少 batch_id'}
    node_name = batch.get('node')
    batch_id = batch['batch_id']

    existed = db.query('SELECT * FROM offline_batches WHERE batch_id=?',
                       (batch_id,))
    if existed:
        return {'ok': True, 'duplicate': True, 'batch_id': batch_id,
                'reason': '批次已回放（幂等去重）'}

    n = db.get_node(node_name)
    if n is None:
        return {'ok': False, 'reason': f'未知节点 {node_name}'}

    tasks = int(batch.get('tasks') or 0)
    tokens = int(batch.get('tokens') or 0)
    seconds = float(batch.get('node_seconds') or 0.0)

    db.add_offline_batch(node_name, batch_id, tasks, tokens, seconds,
                         replayed=bool(replay), note=batch.get('note') or '')

    # 计量补偿：断网期间的算力同样要计费（否则"断网白干"或"断网白算"）
    mode = n_engine_mode(n)
    amount = metering.price(mode, tokens=tokens, node_seconds=seconds)
    db.add_ledger(
        tenant=tenant, task=f'offline:{batch_id}', node=node_name,
        cost=metering.credit_cost(tokens or 1), note='offline-compensate',
        node_seconds=seconds, tokens=tokens, seats=0, billing_mode=mode,
        project='(离线补偿)', department='(离线补偿)',
        amount_cny=amount, estimated=True, engine=engine)
    trust.seal('offline-batch', batch_id,
               f'{node_name}|{tasks}|{tokens}|{seconds}|{amount}')

    # 断网窗口内的在线率轻微折价（诚实口径：断网不是 100% 可用）
    try:
        up = float(n.uptime_ratio or 1.0)
        n.uptime_ratio = round(max(0.0, min(1.0, up * 0.995)), 4)
        db.upsert_node(n)
    except (TypeError, ValueError):
        pass

    db.mark_batch_replayed(batch_id)
    db.add_event('offline-replay',
                 f'{node_name} 批次 {batch_id} 回放完成：{tasks} 任务 / '
                 f'{tokens} tokens / 补偿计费 {amount} 元', node=node_name)
    return {'ok': True, 'batch_id': batch_id, 'node': node_name,
            'tasks': tasks, 'tokens': tokens, 'node_seconds': seconds,
            'billing_mode': mode, 'amount_cny': amount}


def n_engine_mode(node):
    """补偿计费用的口径：默认取配置默认计费模式。"""
    return metering.default_mode()


# ---------------------------------------------------------------- 沙盒模拟
def simulate_offline(node_name, seconds=120, tasks=3, tokens=1500,
                     offline_seconds=None):
    """沙盒：伪造一次"断网 → 本地续跑 → 恢复联网 → 批次回放"。

    不依赖真实网络中断，可直接用于测试与演示验证。
    """
    offline_seconds = float(offline_seconds or seconds)
    started = mark_offline(node_name)
    if started is None:
        return {'ok': False, 'reason': f'未知节点 {node_name}'}
    batch = build_batch(node_name, tasks=tasks, tokens=tokens,
                        node_seconds=offline_seconds,
                        note='沙盒模拟断网批次')
    set_pending(node_name, tasks)
    res = {'ok': True, 'node': node_name, 'offline_seconds': offline_seconds,
           'queue': {'tasks': tasks, 'tokens': tokens},
           'batch': batch}
    if offline_seconds > 0:
        res['note'] = '沙盒：未真实中断网络，仅按给定时长构造批次'
    return res


def recover(node_name, batch=None, tenant='demo'):
    """沙盒：恢复联网并回放批次。"""
    mark_online(node_name)
    if batch is None:
        return {'ok': True, 'node': node_name, 'replayed': False,
                'reason': '无待回放批次'}
    r = ingest_batch(batch, tenant=tenant)
    return {'ok': r.get('ok', False), 'node': node_name, 'replayed': True,
            'batch_result': r}


# ---------------------------------------------------------------- 汇总
def status():
    rows = db.offline_batch_rows(100)
    nodes = [n for n in db.all_nodes() if n.offline]
    return {
        'offline_nodes': [{'node': n.node, 'since': n.offline_since,
                           'pending_local': n.pending_local} for n in nodes],
        'batches': len(rows),
        'replayed': sum(1 for r in rows if r.get('replayed')),
        'tokens_compensated': sum(int(r.get('tokens') or 0) for r in rows),
        'node_seconds_compensated': round(
            sum(float(r.get('node_seconds') or 0) for r in rows), 2),
        'recent': rows[:10],
        'policy': ('边缘本地优先；断网期间任务在本机继续执行，联网后以'
                   '批次为单位回放，控制面做幂等去重与计量补偿，'
                   '并对断网窗口的在线率轻微折价（不谎报 100% 可用）。'),
    }
