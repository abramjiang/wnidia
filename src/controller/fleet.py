# -*- coding: utf-8 -*-
"""具身机队：任务分配 / 模型灰度热更新 / 回传脱敏（BP P18 部分 · 沙盒验证）。

**能力边界**：本模块是**纯软件的机队仿真与编排**，不含机器人本体控制；
真机需 Isaac Sim / Isaac ROS / Holoscan 侧配合。骨架由 WNIDIA 负责：
下发任务、灰度切版本、回收脱敏摘要。

设计借鉴（非拷贝）：
- **EdgeFlow** 的「模型仓库 / 版本管理 / 灰度发布」分层：版本与灰度百分比分离建模。
- **KubeVela** 的「版本化 + 灰度发布」取向：灰度按比例放量，可回滚。
- ROS 2 机队管理常见的「按机队分组 + 区域亲和」调度约束。
本项目把灰度分配做成**确定性哈希**（robot_id % 100 < rollout_pct），
保证同一批次重复计算结果一致——与调度内核的"决策可复现"保持一致。
"""
import hashlib
import re
import time

from . import db, trust

# 回传脱敏规则（正则 → 替换）
REDACT_RULES = (
    (r'\b\d{1,3}(?:\.\d{1,3}){3}\b', '[IP]'),
    (r'\b[\w.+-]+@[\w-]+\.[\w.]+\b', '[EMAIL]'),
    (r'\b1[3-9]\d{9}\b', '[PHONE]'),
    (r'\b\d{17}[\dXx]\b', '[IDNO]'),
    (r'\b(?:\d[ -]?){13,19}\b', '[CARD]'),
    (r'\b(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*\S+',
     '[SECRET]'),
)


def sanitize(text, extra_patterns=None):
    """回传脱敏：把敏感字段替换为占位符，并返回脱敏统计与摘要。"""
    original = '' if text is None else str(text)
    out = original
    hits = {}
    rules = list(REDACT_RULES) + list(extra_patterns or [])
    for pat, rep in rules:
        out, n = re.subn(pat, rep, out, flags=re.IGNORECASE)
        if n:
            hits[rep] = hits.get(rep, 0) + n
    return {
        'text': out, 'redactions': hits,
        'redacted_total': sum(hits.values()),
        'changed': out != original,
        'digest': hashlib.sha256(out.encode('utf-8')).hexdigest(),
    }


# ---------------------------------------------------------------- 机队
def register_robot(robot, fleet='fleet-a', model_name='edge-brain',
                   model_version='v1', battery=100.0, region='local',
                   status='online', note=''):
    db.upsert_robot(robot, fleet=fleet, model_name=model_name,
                    model_version=model_version, status=status,
                    battery=battery, region=region, note=note)
    db.add_event('robot-register',
                 f'{robot} 加入机队 {fleet}（{region}，模型 {model_name}:'
                 f'{model_version}，电量 {battery}%）')
    return db.query('SELECT * FROM robots WHERE robot=?', (robot,))[0]


def robots(fleet=None):
    rows = db.all_robots()
    return [r for r in rows if not fleet or r.get('fleet') == fleet]


# ---------------------------------------------------------------- 模型灰度
def plan_rollout(model_name, version, stages=(10, 50, 100), note=''):
    """登记一次灰度计划：按 stages 依次放量。返回各阶段分配预览。"""
    out = []
    for pct in stages:
        db.add_model_version(model_name, version, 'rolling', int(pct),
                             digest=hashlib.sha256(
                                 f'{model_name}:{version}'.encode()).hexdigest()[:16],
                             note=note)
        dist = _assign(model_name, int(pct))
        out.append({'stage_pct': int(pct), 'assignment': dist})
    trust.seal('model-rollout', f'{model_name}:{version}',
               str(list(stages)))
    db.add_event('model-rollout',
                 f'模型 {model_name} 灰度计划已登记：版本 {version}，'
                 f'阶段 {list(stages)}')
    return {'model_name': model_name, 'version': version,
            'stages': out, 'stages_planned': list(stages)}


def _assign(model_name, rollout_pct):
    """确定性灰度分配：robot 哈希 % 100 < rollout_pct 的切到新版本。"""
    cur = db.current_model_rollout(model_name)
    new_ver = cur['version'] if cur else 'v1'
    old_ver = 'v0' if new_ver != 'v0' else 'v1'
    dist = {'new_version': [], 'old_version': []}
    for r in db.all_robots():
        if r.get('model_name') != model_name:
            continue
        h = int(hashlib.sha1(str(r['robot']).encode()).hexdigest()[:8], 16)
        (dist['new_version'] if h % 100 < rollout_pct
         else dist['old_version']).append(r['robot'])
    return {'rollout_pct': rollout_pct,
            # 带上版本名：机队控制台才说得清"哪些机器人升到哪个版本"，
            # 否则只看到 robot id，演示时无法自证灰度目标版本（原 old_ver 死变量）。
            'new_version_name': new_ver, 'old_version_name': old_ver,
            'new_version': sorted(dist['new_version']),
            'old_version': sorted(dist['old_version']),
            'new_count': len(dist['new_version']),
            'old_count': len(dist['old_version'])}


def rollout_now(model_name, version, rollout_pct, dry_run=False):
    """把灰度比例落到机队（写 robots.model_version）。dry_run 只预览不写入。"""
    if not (0 <= int(rollout_pct) <= 100):
        return {'ok': False, 'reason': 'rollout_pct 需在 0..100'}
    db.add_model_version(model_name, version, 'rolling', int(rollout_pct),
                         digest=hashlib.sha256(
                             f'{model_name}:{version}'.encode()).hexdigest()[:16],
                         note='manual rollout')
    dist = _assign(model_name, int(rollout_pct))
    if not dry_run:
        for rid in dist['new_version']:
            db.execute('UPDATE robots SET model_version=? WHERE robot=?',
                       (version, rid))
        for rid in dist['old_version']:
            rows = db.query('SELECT * FROM robots WHERE robot=?', (rid,))
            if rows and rows[0].get('model_version') == version:
                db.execute('UPDATE robots SET model_version=? WHERE robot=?',
                           ('previous', rid))
        trust.seal('model-rollout-apply', f'{model_name}:{version}',
                   f'{rollout_pct}|{dist["new_count"]}')
        db.add_event('model-rollout',
                     f'{model_name} 灰度放量到 {rollout_pct}%：'
                     f'{dist["new_count"]} 台切新版本')
    return {'ok': True, 'model_name': model_name, 'version': version,
            'rollout_pct': int(rollout_pct), 'dry_run': bool(dry_run),
            'assignment': dist}


# ---------------------------------------------------------------- 任务分配
def dispatch_tasks(tasks, fleet=None, min_battery=15.0):
    """把一批任务分给机队：优先电量高、同区域、当前负载低的机器人。

    tasks: [{'task':'t1','region':'local','need':'vision'}]
    """
    cand = [r for r in robots(fleet)
            if (r.get('status') == 'online'
                and float(r.get('battery') or 0) >= min_battery)]
    if not cand:
        return {'ok': False, 'reason': '无机队成员满足电量与在线条件',
                'assigned': [], 'unassigned': [t.get('task') for t in tasks]}

    load = {r['robot']: 0 for r in cand}
    assigned, unassigned = [], []
    for t in tasks or []:
        region = t.get('region')
        pool = [r for r in cand if not region or r.get('region') == region] or cand
        pool.sort(key=lambda r: (-(float(r.get('battery') or 0)),
                                 load[r['robot']], str(r['robot'])))
        pick = pool[0]
        load[pick['robot']] += 1
        assigned.append({'task': t.get('task'), 'robot': pick['robot'],
                         'fleet': pick.get('fleet'), 'region': pick.get('region'),
                         'model_version': pick.get('model_version'),
                         'reason': f'电量 {pick.get("battery")}%、'
                                   f'当前分配 {load[pick["robot"]]} 个任务'})
    db.add_event('fleet-dispatch',
                 f'机队派发 {len(assigned)} 个任务，'
                 f'未分配 {len(unassigned)} 个')
    return {'ok': True, 'assigned': assigned, 'unassigned': unassigned,
            'candidates': len(cand)}


def ingest_uplink(robot, payload):
    """接收机队回传：先脱敏，再落事件（原始数据不进控制面）。"""
    s = sanitize(payload)
    db.execute('UPDATE robots SET last_seen=?, tasks_done=tasks_done+1 '
               'WHERE robot=?', (int(time.time() * 1000), robot))
    trust.seal('fleet-uplink', robot, s['digest'])
    db.add_event('fleet-uplink',
                 f'{robot} 回传已脱敏（替换 {s["redacted_total"]} 处，'
                 f'digest={s["digest"][:12]}…）')
    return {'ok': True, 'robot': robot, 'sanitized': s}


def status():
    rs = db.all_robots()
    by_fleet = {}
    for r in rs:
        b = by_fleet.setdefault(r.get('fleet') or '(未分组)',
                                {'fleet': r.get('fleet'), 'total': 0,
                                 'online': 0, 'avg_battery': 0.0})
        b['total'] += 1
        if r.get('status') == 'online':
            b['online'] += 1
        b['avg_battery'] += float(r.get('battery') or 0)
    for b in by_fleet.values():
        b['avg_battery'] = round(b['avg_battery'] / max(b['total'], 1), 1)
    return {'robots': len(rs), 'fleets': sorted(by_fleet.values(),
                                                key=lambda x: -(x['total'])),
            'model_versions': db.model_version_rows(20),
            'redact_rules': len(REDACT_RULES),
            'capability_note': ('纯软件机队仿真与编排；本体控制与感知由 '
                                'Isaac ROS / Holoscan 侧承担，本项目不做。')}
