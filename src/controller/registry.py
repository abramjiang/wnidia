# -*- coding: utf-8 -*-
"""L1 纳管：设备主动注册 + 心跳（绝不主动扫描）。

合规要点（手册 8.1-2）：所有连接均为**出站方向**，由节点主动注册与心跳，
控制面从不发起内网探测——这也是本设计"天然规避扫描红线"的原因。

隔离期（quarantine）：控制面判定节点离线后，短时间内**不再**被心跳"复活"，
避免演示注入（/admin/node/lost）后 5 秒又自己上线，导致重调度与恢复逻辑看不清。
"""
from . import config, db
from .models import NodeProfile, NodeStatus, now_ms

# node -> 解除隔离的时间戳（毫秒）
_QUARANTINE = {}


def is_quarantined(node):
    until = _QUARANTINE.get(node, 0)
    if until and until <= now_ms():
        _QUARANTINE.pop(node, None)
        return False
    return bool(until)


def register(p: NodeProfile) -> NodeProfile:
    existing = db.get_node(p.node)
    _QUARANTINE.pop(p.node, None)          # 主动重新注册即视为人工恢复
    if existing is None:
        p.status = NodeStatus.ONLINE.value
        p.last_heartbeat = now_ms()
        p.free_mem_gb = p.mem_limit_gb
        db.upsert_node(p)
        db.add_event('register', f'{p.node} 主动注册上线'
                                f'（{p.tier}/{p.role}/{p.engine}）', node=p.node)
    else:
        # 重复注册：以新载荷覆盖静态画像（幂等），保留运行期字段（信誉/利用率等）
        for f in ('role', 'tier', 'mem_total_gb', 'mem_limit_gb', 'compute_pct',
                  'vllm_port', 'trusted', 'sovereign_region', 'gpu_name'):
            setattr(existing, f, getattr(p, f))
        existing.engine = p.engine or existing.engine
        existing.status = NodeStatus.ONLINE.value
        existing.last_heartbeat = now_ms()
        existing.free_mem_gb = min(existing.free_mem_gb, existing.mem_limit_gb)
        db.upsert_node(existing)
        db.add_event('register', f'{p.node} 重新注册上线（画像已更新）',
                     node=p.node)
        p = existing
    db.ensure_quota('demo', 1000)
    return p


def heartbeat(node, util, free_mem_gb, kv_hit=0.0, engine=None,
              engine_healthy=None, degraded=None, latency_ms=None,
              uptime_ratio=None, stability=None, offline=None,
              pending_local=None):
    p = db.get_node(node)
    if p is None:
        return False
    p.util = float(util)
    p.free_mem_gb = float(free_mem_gb)
    p.kv_hit = float(kv_hit)
    p.last_heartbeat = now_ms()
    if engine:
        p.engine = engine
    if engine_healthy is not None:
        p.engine_healthy = bool(engine_healthy)
    if degraded is not None:
        p.degraded = bool(degraded)
    # v4 画像维度（运行期可变项）
    for field, val in (('latency_ms', latency_ms),
                       ('uptime_ratio', uptime_ratio),
                       ('stability', stability)):
        if val is None:
            continue
        try:
            setattr(p, field, float(val))
        except (TypeError, ValueError):
            pass
    if offline is not None:
        p.offline = bool(offline)
    if pending_local is not None:
        try:
            p.pending_local = max(0, int(pending_local))
        except (TypeError, ValueError):
            pass
    # 隔离期内只更新指标，不改变在线状态
    if not is_quarantined(node):
        p.status = NodeStatus.ONLINE.value
    db.upsert_node(p)
    return True


def mark_status(node, status, quarantine_s=None):
    p = db.get_node(node)
    if p:
        p.status = status
        db.upsert_node(p)
        if status == NodeStatus.LOST.value:
            secs = quarantine_s if quarantine_s is not None \
                else config.HEARTBEAT_TIMEOUT_S
            _QUARANTINE[node] = now_ms() + int(secs) * 1000
    return p


def set_cheat(node, cheat: bool):
    p = db.get_node(node)
    if p:
        p.cheat = cheat
        db.upsert_node(p)
    return p
