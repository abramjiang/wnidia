# -*- coding: utf-8 -*-
"""L2/L3：准入 admit、多目标匹配 match、绑定、checkpoint、多数决校验。

决策全部为确定性 Python，模型（LLM）无法绕过。

v4 补齐（BP P5「按时延/成本/密级三维择点」）：
- **第三维「时延」**：节点上报 `latency_ms`；`latency_budget_ms > 0` 时作为**硬约束**，
  超标节点直接不入候选；未设预算时进入打分项（越低越优）。
- **密级 → 机密计算层级联动**：L3/L4 任务除要求可信节点外，还要求节点
  `cc_level` 达标且持有有效证明（见 trust.check_node）。
- **SLA 优先级排队**：出队顺序由 `db.queued_tasks_ordered()` 保证（sla-1 优先）。
"""
from dataclasses import replace
from typing import List, Optional

from . import capabilities, db, trust
from .models import (NodeProfile, TaskSpec, ScheduleResult, Admission,
                     NodeStatus, NodeTier, SLA, Secret, TaskState, now_ms)

SECRET_RANK = {'L1': 1, 'L2': 2, 'L3': 3, 'L4': 4}


def rank_of(secret) -> int:
    """密级取值容错：非法值按最低密级处理，避免 KeyError 打断调度闭环。"""
    return SECRET_RANK.get(secret, 1)


def want_role_for(t: TaskSpec) -> str:
    return 'prefill' if (t.task_type == 'heavy' or t.tokens_in > 1500) \
        else 'decode'


# ---------------- 准入 ----------------
def admit(t: TaskSpec) -> Admission:
    q = db.get_quota(t.tenant)
    if q['used'] + t.cost > q['quota']:
        return Admission.REJECT_QUOTA
    # 机密/主权：L3/L4 必须存在可信且同主权地的在线节点
    if rank_of(t.secret) >= SECRET_RANK['L3']:
        if not any(n.status == NodeStatus.ONLINE.value and n.trusted
                   and n.sovereign_region == t.sovereign_region
                   for n in db.all_nodes()):
            return Admission.REJECT_SECRET
    if not _candidates(t):
        # 区分两种"没候选"：
        #   ① 时延预算把候选全筛掉了（三维路由第三维硬约束失败）——
        #      把预算放宽后仍有候选，说明是时延问题；
        #   ② 真的没有可用算力。
        # 不区分的话，看板与答辩都会把 ① 说成"机器不够"，方向就错了。
        budget = float(getattr(t, 'latency_budget_ms', 0) or 0)
        if budget > 0 and _candidates(replace(t, latency_budget_ms=0.0)):
            return Admission.REJECT_LATENCY
        return Admission.REJECT_CAP
    return Admission.OK


# ---------------- 候选（含三维硬约束） ----------------
def _latency_ok(n: NodeProfile, t: TaskSpec):
    budget = float(getattr(t, 'latency_budget_ms', 0) or 0)
    if budget <= 0:
        return True, ''
    lat = float(getattr(n, 'latency_ms', 0) or 0)
    if lat <= 0:
        # 未上报时延的节点在"有时延硬约束"的任务上不可信，直接排除
        return False, f'{n.node} 未上报时延，无法满足 {budget}ms 硬约束'
    if lat > budget:
        return False, f'{n.node} 时延 {lat}ms > 预算 {budget}ms'
    return True, ''


def _candidates(t: TaskSpec) -> List[NodeProfile]:
    out = []
    for n in db.all_nodes():
        if n.status != NodeStatus.ONLINE.value:
            continue
        # 作弊节点（演示注入或多数决已标记）不参与单任务派发——
        # 它仍会被多数决拉来做对端比对（_verify_majority 走 all_nodes，
        # 不经过本函数），即「可以被抓、不能被派活」。此前不过滤时，
        # off 档单任务会被直接派给作弊节点且结果被采信，等于多数决
        # 防护只对主动开 verify 的用户生效。
        if getattr(n, 'cheat', False):
            continue
        if n.free_mem_gb + 0.05 < t.need_mem_gb:
            continue
        if n.sovereign_region != t.sovereign_region and \
                rank_of(t.secret) >= SECRET_RANK['L2']:
            continue
        if rank_of(t.secret) >= SECRET_RANK['L3'] and not n.trusted:
            continue
        ok, _why = _latency_ok(n, t)
        if not ok:
            continue
        # 机密计算层级准入（BP P9）：不达标或证明无效则不入候选
        cc_ok, _req, _detail = trust.check_node(n, t)
        if not cc_ok:
            continue
        out.append(n)
    return out


def reject_reasons(t: TaskSpec):
    """把"为什么没有候选"讲清楚——按优先级给出每类拒绝原因的计数。"""
    reasons = {}
    for n in db.all_nodes():
        if n.status != NodeStatus.ONLINE.value:
            reasons['节点离线'] = reasons.get('节点离线', 0) + 1
            continue
        if getattr(n, 'cheat', False):
            reasons['节点不可信(作弊标记)'] = reasons.get('节点不可信(作弊标记)', 0) + 1
            continue
        if n.free_mem_gb + 0.05 < t.need_mem_gb:
            reasons['空闲显存不足'] = reasons.get('空闲显存不足', 0) + 1
            continue
        if n.sovereign_region != t.sovereign_region and \
                rank_of(t.secret) >= SECRET_RANK['L2']:
            reasons['数据主权区不匹配'] = reasons.get('数据主权区不匹配', 0) + 1
            continue
        if rank_of(t.secret) >= SECRET_RANK['L3'] and not n.trusted:
            reasons['L3+ 要求可信节点'] = reasons.get('L3+ 要求可信节点', 0) + 1
            continue
        ok, why = _latency_ok(n, t)
        if not ok:
            reasons['时延超预算'] = reasons.get('时延超预算', 0) + 1
            continue
        cc_ok, req, _d = trust.check_node(n, t)
        if not cc_ok:
            reasons[f'机密计算层级不足({req})'] = \
                reasons.get(f'机密计算层级不足({req})', 0) + 1
            continue
        reasons['可用'] = reasons.get('可用', 0) + 1
    return reasons


# ---------------- 多目标打分 ----------------
def score(n: NodeProfile, t: TaskSpec) -> float:
    want_role = want_role_for(t)
    s = 0.0
    # 角色匹配
    s += 22 if n.role == want_role else 4
    # 实时负载（利用率越低越优先）
    s += (100 - min(n.util, 100)) * 0.32
    # 显存余量
    s += min(n.free_mem_gb, 12) * 0.8
    # KV 命中
    s += n.kv_hit * 12
    # 信誉
    s += n.reputation * 10
    # 时延（三维路由第三维）：0ms 视为未知，给中性分；越低越高分
    lat = float(getattr(n, 'latency_ms', 0) or 0)
    if lat > 0:
        # 20ms 以内满分 12，200ms 以上得 0 分，线性衰减
        s += max(0.0, min(12.0, (200.0 - min(lat, 200.0)) / 180.0 * 12.0))
    else:
        s += 6.0
    # 稳定性与在线率（BP P7 能力画像分级）
    try:
        s += min(max(float(n.stability or 0), 0.0), 1.0) * 6
        s += min(max(float(n.uptime_ratio or 0), 0.0), 1.0) * 4
    except (TypeError, ValueError):
        pass
    # 机密计算层级（高密级任务偏好能力更强的节点）
    if rank_of(t.secret) >= SECRET_RANK['L3']:
        s += capabilities.CC_RANK.get(getattr(n, 'cc_level', 'CC-L0'), 0) * 6
    # SLA 档位偏好
    if t.sla in (SLA.S1.value, SLA.S2.value):
        s += {'cloud': 14, 'edge': 6, 'home': 0, 'cpu': -6}.get(n.tier, 0)
    else:  # spot：优先便宜的边缘/端侧
        s += {'cloud': -4, 'edge': 8, 'home': 12, 'cpu': 10}.get(n.tier, 0)
    # 端侧节点承接强实时任务应被惩罚（BP P7：强实时任务留在中心）
    if n.tier in ('home', 'cpu') and t.sla == SLA.S1.value:
        s -= 25
    return round(s, 2)


def match(t: TaskSpec, prefer: Optional[str] = None) -> Optional[NodeProfile]:
    """选节点。prefer：Agent 提议的目标节点（可选）。

    prefer 的约束：必须是 _candidates 的成员（生成候选时硬约束已全部执行），
    因此「采纳提议」永远不会绕过准入/显存/主权/密级/时延/机密任何一条——
    这是 agent_policy 裁决器的合法性来源，两处口径同源不漂移。
    """
    cands = _candidates(t)
    if not cands:
        return None
    if prefer:
        for n in cands:
            if n.node == prefer:
                return n
    cands.sort(key=lambda n: (-score(n, t), n.node))
    return cands[0]


def _reasons_brief(t: TaskSpec, top=3) -> str:
    """把拒绝原因压成一行，便于事件流与答辩页直接引用。"""
    rs = reject_reasons(t)
    rs.pop('可用', None)
    if not rs:
        return '无可用节点'
    items = sorted(rs.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return '、'.join(f'{k}×{v}' for k, v in items)


def schedule(t: TaskSpec, prefer_node: Optional[str] = None) -> ScheduleResult:
    """prefer_node：Agent 策略（agent_policy.decide）提议并通过裁决的节点。"""
    a = admit(t)
    if a != Admission.OK:
        t.state = TaskState.REJECTED.value
        t.error = a.value
        db.upsert_task(t)
        extra = ''
        if a == Admission.REJECT_LATENCY:
            # 拒绝原因必须能直接指向"时延预算"，而不是笼统的"没算力"
            extra = (f'（时延预算 {float(t.latency_budget_ms or 0):.3f}ms '
                     f'无节点可满足：{_reasons_brief(t)}）')
        db.add_event('reject', f'{t.task} 被拒：{a.value}{extra}', task=t.task)
        return ScheduleResult(admitted=a, reason=a.value)
    n = match(t, prefer=prefer_node)
    if n is None:
        return ScheduleResult(admitted=Admission.REJECT_CAP, reason='no_node')
    t.node = n.node
    t.state = TaskState.BINDING.value
    t.bound_at = now_ms()
    db.upsert_task(t)
    db.add_event('bind',
                 f'{t.task} 绑定到 {n.node}（score={score(n, t):.1f}，'
                 f'时延={float(n.latency_ms or 0):.1f}ms，'
                 f'机密层级={n.cc_level}）', node=n.node, task=t.task)
    return ScheduleResult(admitted=Admission.OK, node=n.node)


# ---------------- 抢占 / checkpoint ----------------
def preempt(t: TaskSpec, by_task: str):
    """高优任务抢占 spot 任务：保存 checkpoint，任务转 preempted。"""
    ckpt = {
        'task': t.task, 'node': t.node, 'progress': t.progress,
        'answer': t.answer, 'ts': now_ms(),
    }
    t.state = TaskState.PREEMPTED.value
    db.upsert_task(t)
    db.add_event('preempt', f'{t.task} 被 {by_task} 抢占，已存 checkpoint'
                            f'（progress={t.progress:.0%}）',
                 node=t.node, task=t.task)
    return ckpt


def resume(t: TaskSpec, node):
    t.node = node.node if isinstance(node, NodeProfile) else node
    t.state = TaskState.QUEUED.value
    db.upsert_task(t)
    db.add_event('resume', f'{t.task} 从 checkpoint 恢复到 {t.node}'
                           f'（progress={t.progress:.0%}）',
                 node=t.node, task=t.task)


# ---------------- 多数决校验 ----------------
def majority(answers: List[str]):
    """多数决：返回 (多数答案, 是否完全一致)。

    第二个值表示是否全员一致；只要有少数派即返回 False，以便定位异常节点。
    **确定性**：先按出现次数、再按字典序排序，平票结果可复现。
    """
    if not answers:
        return '', False
    counts = {}
    for a in answers:
        counts[a] = counts.get(a, 0) + 1
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, counts[best] == len(answers)


# ---------------- 分时复用（BP P13 L2） ----------------
def time_slice_plan(nodes=None, window_minutes=60, slice_minutes=15,
                    tenants=None):
    """把窗口切成等长时片，按租户轮转分配——分时复用的**策略侧**。

    执行侧（MPS/MIG 真实隔离）依赖硬件，本函数只产出可解释的排期，
    并在输出中标注 `execution='policy_only'`。
    """
    nodes = nodes if nodes is not None else db.all_nodes()
    tenants = list(tenants or ['demo'])
    if slice_minutes <= 0 or window_minutes <= 0:
        return {'ok': False, 'reason': '窗口与时片必须为正数'}
    if slice_minutes > window_minutes:
        return {'ok': False, 'reason': '时片不能大于窗口'}
    n_slices = int(window_minutes // slice_minutes)
    if not tenants:
        return {'ok': False, 'reason': '至少需要一个租户'}
    plan = []
    for i in range(n_slices):
        start = i * slice_minutes
        plan.append({
            'slice': i, 'from_min': start, 'to_min': start + slice_minutes,
            'tenant': tenants[i % len(tenants)],
        })
    share = {t: round(sum(1 for p in plan if p['tenant'] == t) / len(plan), 4)
             for t in tenants}
    return {'ok': True, 'window_minutes': window_minutes,
            'slice_minutes': slice_minutes, 'slices': plan,
            'share': share, 'nodes': [n.node for n in nodes],
            'execution': 'policy_only',
            'note': ('仅产出分时排期策略；MPS/MIG 的真实隔离执行依赖硬件，'
                     'GB10 不支持 MIG。')}
