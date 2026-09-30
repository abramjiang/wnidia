# -*- coding: utf-8 -*-
"""L3/L4 Harness 闭环：感知—匹配—执行—回流（后台常驻）。

v4 补齐：
- 出队顺序改用 `db.queued_tasks_ordered()`（SLA 优先级排队，BP P10）；
- 完成时走 `metering.record()` 落真实计量与三口径金额（BP P10）；
- 采集引擎返回的 usage（worker 透传）写入任务，供计量与门槛使用；
- 关键状态变迁写入审计哈希链（BP P9 CC-L3）。
"""
import asyncio

from . import (config, db, registry, scheduler, executor, httpcli, jevclient,
               metering, prompt_guard, trust, agent_policy)
from .models import (TaskSpec, TaskState, NodeStatus, SLA, now_ms, ScheduleResult)

# preemptor_task -> [preempted tasks]，preemptor 完成后自动恢复
RESUME_AFTER = {}


def _worker(n, path, **kw):
    return httpcli.post(f'{executor.worker_base(n)}{path}', **kw)


def _reduce_reputation(node, by=0.2):
    n = db.get_node(node)
    if n:
        n.reputation = max(0.0, n.reputation - by)
        db.upsert_node(n)


# ---------------- 节点掉线 ----------------
def detect_lost():
    ts = now_ms()
    for n in db.all_nodes():
        if n.status == NodeStatus.ONLINE.value and n.last_heartbeat and \
                ts - n.last_heartbeat > config.HEARTBEAT_TIMEOUT_S * 1000:
            registry.mark_status(n.node, NodeStatus.LOST.value)
            db.add_event('lost', f'{n.node} 心跳超时，判定离线', node=n.node)
            # 在途任务重调度
            for t in db.all_tasks():
                if t.node == n.node and t.state in (TaskState.RUNNING.value,
                                                    TaskState.BINDING.value):
                    if t.attempts + 1 >= config.MAX_ATTEMPTS:
                        t.state = TaskState.FAILED.value
                        t.error = f'重排超过 {config.MAX_ATTEMPTS} 次'
                        t.finished_at = now_ms()
                        db.upsert_task(t)
                        db.add_event('fail',
                                     f'{t.task} 多次重排失败，终止', task=t.task)
                        prompt_guard.forget(t.task)
                        continue
                    t.state = TaskState.QUEUED.value; t.node = None
                    t.attempts += 1
                    db.upsert_task(t)
                    db.add_event('redispatch',
                                 f'{t.task} 因节点掉线重新排队', task=t.task)


# ---------------- 派发前置：prompt 是否真的可用 ----------------
def _dispatchable(t: TaskSpec):
    """派发前确认手里是**真实 prompt**，而不是隐私占位标记。

    为什么必须有这道闸（v5 隐私改造的配套保险）：
        默认不落库明文后，prompt 只在控制面进程内存里。控制面重启后，
        未终态任务的 prompt 就没了 —— 如果直接派发，worker 会把
        `[REDACTED:prompt-not-stored]` 当成真实输入去推理，产生**假结果**
        并且一路走到"完成"，这是最危险的一种错：不报错，只是答案是错的。
        所以这里显式拦下，让这类任务判失败（error=prompt_unavailable），
        而不是拿占位符去跑。
    """
    if prompt_guard.is_redacted(t.prompt):
        return False, 'prompt_unavailable'
    if not t.prompt:
        return False, 'prompt_empty'
    return True, ''


# ---------------- 启动排队任务 ----------------
def launch_queued():
    # SLA 优先级排队：sla-1 → sla-2 → sla-3，同级 FIFO（原实现只按创建时间）
    for t in db.queued_tasks_ordered()[:6]:
        ready, why = _dispatchable(t)
        if not ready:
            # prompt 不可用属于**终态错误**：重排多少次都不会好，直接失败
            t.state = TaskState.FAILED.value
            t.error = why
            t.finished_at = now_ms()
            db.upsert_task(t)
            db.add_event('fail',
                         f'{t.task} 无法派发（{why}）：prompt 不落库明文，'
                         f'控制面重启后内存副本已失效', task=t.task)
            prompt_guard.forget(t.task)
            continue
        # Agent 决策策略（v5.1）：off=None 零开销；shadow=双轨留痕；
        # enforce=提议通过裁决才返回 prefer 节点。任何异常都回落内核选路。
        try:
            prefer = agent_policy.decide(t)
        except Exception:                   # noqa: BLE001  策略层故障不影响调度
            prefer = None
        res: ScheduleResult = scheduler.schedule(t, prefer_node=prefer)
        if res.admitted.value != 'ok' or not res.node:
            continue
        n = db.get_node(res.node)
        try:
            r = _worker(n, '/start', json={
                'task': t.task, 'prompt': t.prompt,
                'task_type': t.task_type, 'tokens_in': t.tokens_in})
            if not (r.json() or {}).get('ok'):
                raise RuntimeError(f'worker 拒绝启动：{r.text[:80]}')
            t.state = TaskState.RUNNING.value
            db.upsert_task(t)
        except Exception as e:
            # 原实现无重试上限：worker 长期不可用时该任务会每 2 秒重排一次，
            # 永远停在 queued，看板显示"卡住"。这里补上 MAX_ATTEMPTS 收口。
            t.attempts += 1
            t.error = str(e)[:80]
            if t.attempts >= config.MAX_ATTEMPTS:
                t.state = TaskState.FAILED.value
                t.finished_at = now_ms()
                db.add_event('fail',
                             f'{t.task} 启动重试超过 {config.MAX_ATTEMPTS} 次，终止'
                             f'（{t.error}）', task=t.task)
                prompt_guard.forget(t.task)
            else:
                t.state = TaskState.QUEUED.value; t.node = None
            db.upsert_task(t)


# ---------------- 回收卡在 binding 的任务 ----------------
def reclaim_stuck_binding():
    """绑定后启动失败/无响应的任务必须有归宿，否则永久停在 binding。"""
    ts = now_ms()
    limit = config.BINDING_TIMEOUT_S * 1000
    for t in db.tasks_by_state(TaskState.BINDING.value):
        if not t.bound_at or ts - t.bound_at < limit:
            continue
        if t.attempts + 1 >= config.MAX_ATTEMPTS:
            t.state = TaskState.FAILED.value
            t.error = 'binding 超时且重排已达上限'
            t.finished_at = ts
            db.add_event('fail', f'{t.task} 绑定超时，重排已达上限，终止',
                         task=t.task)
            prompt_guard.forget(t.task)
        else:
            t.state = TaskState.QUEUED.value
            t.node = None
            t.attempts += 1
            db.add_event('redispatch', f'{t.task} 绑定超时，重新排队',
                         task=t.task)
        db.upsert_task(t)


# ---------------- 轮询运行中任务 ----------------
def poll_running():
    for t in db.tasks_by_state(TaskState.RUNNING.value):
        n = db.get_node(t.node) if t.node else None
        if n is None:
            t.state = TaskState.QUEUED.value; t.node = None
            db.upsert_task(t); continue
        try:
            r = httpcli.get(f'{executor.worker_base(n)}/status',
                            params={'task': t.task}, timeout=10)
            st = r.json()
        except Exception:
            continue
        t.progress = float(st.get('progress', 0))
        db.upsert_task(t)   # 进度落库：否则看板上运行中任务进度恒为 0
        if st.get('state') == 'done':
            t.answer = st.get('answer', '')
            # v4：采集真实用量（worker 透传引擎 usage；缺失则标 estimated）
            # v5：prompt 长度优先取库里落的 prompt_chars —— 不存明文时
            #     t.prompt 仍是内存副本，但一旦被释放，长度不会丢。
            m = metering.meter_from_usage(
                st.get('usage'),
                prompt_chars=int(t.prompt_chars or len(t.prompt or '')),
                answer_chars=len(t.answer or ''))
            t.prompt_tokens = m['prompt_tokens']
            t.completion_tokens = m['completion_tokens']
            t.metering_estimated = bool(m['estimated'])
            if st.get('engine'):
                t.engine = str(st.get('engine'))[:64]
            _complete(t, n)
        elif st.get('state') == 'unknown':
            t.state = TaskState.QUEUED.value; t.node = None; t.attempts += 1
            db.upsert_task(t)


def _verify_majority(t: TaskSpec, n):
    """对 verify 任务做 3 节点多数决，识别作弊节点。"""
    ready, why = _dispatchable(t)
    if not ready:
        # prompt 不可用时不比对：拿占位符发给对端节点，会把"三个节点回答同一段
        # 占位文本"当成"一致"，等于用假数据给自己背书。宁可显式说明未校验。
        db.add_event('verify',
                     f'{t.task} 跳过多数决（{why}）：prompt 不落库明文，'
                     f'内存副本已释放，无法向对端节点复现同一输入',
                     task=t.task)
        return
    payload = {'task': t.task, 'prompt': t.prompt,
               'task_type': t.task_type, 'tokens_in': t.tokens_in}
    answers = [(n.node, t.answer)]
    for peer in db.all_nodes():
        if peer.node == n.node or peer.status != NodeStatus.ONLINE.value:
            continue
        if peer.free_mem_gb < t.need_mem_gb:
            continue
        try:
            r = httpcli.post(f'{executor.worker_base(peer)}/execute',
                             json=payload, timeout=25)
            j = r.json()
            if j.get('ok'):
                answers.append((peer.node, j['answer']))
        except Exception:
            pass
        if len(answers) >= 3:
            break
    vals = [a for _, a in answers]
    # 多数决必须有 >=3 个独立结果才有意义；2 个且不一致时不存在多数，
    # 不能按集合顺序武断惩罚任一节点。
    if len(answers) < 3:
        db.add_event('verify',
                     f'{t.task} 可比对节点不足（{len(answers)}），无法多数决，'
                     f'不做信誉惩罚', task=t.task)
        t.answer = answers[0][1]
        return
    best, exact_unanimous = scheduler.majority(vals)
    judged = jevclient.judge_answers(t.prompt, best, answers)
    if judged and judged.get('trust'):
        # Jev 语义判定可信：按“语义等价 + 证据支持 + 标签”定位异常节点，
        # 措辞不同但语义等价的诚实答案不再被误罚。
        divergent = [node for node, v in judged['items'].items()
                     if v['divergent']]
        if not divergent:
            db.add_event('verify',
                         f'{t.task} 语义校验一致（{len(answers)} 节点，Jev 语义判定）',
                         task=t.task)
        for node_name in divergent:
            v = judged['items'][node_name]
            _reduce_reputation(node_name)
            db.add_event('verify',
                         f'{node_name} 语义校验不一致（{v["label"]}，'
                         f'等价={v["equivalent"]}，支持={v["supported"]}），已降信誉分',
                         node=node_name, task=t.task)
    else:
        # 确定性精确匹配兜底：Jev 关闭 / 不可用 / 低置信 / 真实 Jev 中文场景
        if judged and not judged.get('trust'):
            db.add_event('verify',
                         f'{t.task} Jev 语义判定不可信，回退精确匹配', task=t.task)
        for node_name, ans in answers:
            if ans != best:
                _reduce_reputation(node_name)
                db.add_event('verify',
                             f'{node_name} 结果与多数不一致，已降信誉分',
                             node=node_name, task=t.task)
        if exact_unanimous:
            db.add_event('verify',
                         f'{t.task} 多数决校验一致（{len(answers)} 节点）',
                         task=t.task)
    t.answer = best


def _complete(t: TaskSpec, n):
    if t.verify:
        # 显式要求校验：始终执行多数决（语义增强），不做跳过
        _verify_majority(t, n)
    elif config.JEV_ADAPTIVE:
        # 普通任务：廉价预检答案正确概率，仅存疑时才补做三节点多数决
        ready, why = _dispatchable(t)
        pre = jevclient.precheck_answer(t.prompt, t.answer) if ready else None
        if not ready:
            # 预检要拿 prompt 与答案比对；prompt 不可用时不能瞎判，如实记录跳过
            db.add_event('verify',
                         f'{t.task} 跳过自适应预检（{why}），不劣化为"已校验"',
                         task=t.task)
        elif pre is None:
            pass   # 无法预检：保持原行为，不额外校验、不劣化
        elif pre['p_correct'] >= config.JEV_VERIFY_TRIGGER:
            jevclient.C['skips'] += 1
            db.add_event('verify',
                         f'{t.task} 自适应预检通过（P={pre["p_correct"]}），免多节点校验',
                         task=t.task)
        else:
            jevclient.C['adaptive_verify'] += 1
            db.add_event('verify',
                         f'{t.task} 自适应预检存疑（P={pre["p_correct"]}），补做多数决',
                         task=t.task)
            _verify_majority(t, n)
    t.state = TaskState.DONE.value
    t.progress = 1.0; t.finished_at = now_ms()
    # 一致性留痕：exact 引擎才有逐字节保证，非 exact 引擎显式置 False
    if not t.exact_required and t.consistency_ok is None:
        t.consistency_ok = None
    db.upsert_task(t)
    # v4：真实计量与三口径金额（替代原先的固定 cost 记账）
    node_seconds = max(0.0, (t.finished_at - (t.bound_at or t.created_at)) / 1000.0)
    seats = 1 if t.billing_mode == 'seat' else 0
    bill = metering.record(t, n.node, node_seconds=node_seconds, seats=seats,
                           engine=t.engine or n.engine)
    trust.seal('task-done', t.task,
               f'{t.tenant}|{bill["mode"]}|{bill["tokens"]}|{bill["amount_cny"]}')
    db.add_event('done',
                 f'{t.task} 在 {n.node} 完成并计量（{bill["mode"]}，'
                 f'{bill["tokens"]} tokens，{bill["amount_cny"]} 元'
                 f'{"，估算计量" if bill["estimated"] else ""}）',
                 node=n.node, task=t.task)
    # v5：任务到终态即释放内存里的 prompt 副本（"数据不留中心"的落地动作）
    prompt_guard.forget(t.task)
    # 恢复被本任务抢占的 spot
    for preempted in RESUME_AFTER.pop(t.task, []):
        pt = db.get_task(preempted)
        if pt and pt.state == TaskState.PREEMPTED.value:
            orig = db.get_node(pt.node)
            if orig and orig.status == NodeStatus.ONLINE.value:
                try:
                    _worker(orig, '/resume', params={'task': pt.task})
                except Exception:
                    pass
                scheduler.resume(pt, orig)
                pt.state = TaskState.RUNNING.value
                db.upsert_task(pt)


# ---------------- 抢占（由 admin 注入） ----------------
def inject_preemption(high: TaskSpec):
    """让高优任务抢占一个正在运行的 S3 spot 任务所在节点。"""
    ready, why = _dispatchable(high)
    if not ready:
        # 抢占是一条会改别人任务状态的旁路，同样不能拿占位符去跑
        return False, f'高优任务无法派发（{why}）'
    spot = None
    for t in db.tasks_by_state(TaskState.RUNNING.value):
        if t.sla == SLA.S3.value:
            spot = t; break
    if spot is None:
        return False, '当前没有运行中的 spot 任务'
    n = db.get_node(spot.node)
    try:
        r = _worker(n, '/preempt', params={'task': spot.task})
        ck = r.json()
    except Exception as e:
        return False, str(e)
    if not ck.get('ok'):
        # worker 未能真正抢占（spot 已结束/处于收尾窗口），中止本次抢占
        return False, 'spot 已结束或无法抢占'
    # 用 worker 侧真实进度回写，保证 checkpoint/恢复进度准确
    spot.progress = float(ck.get('progress', spot.progress))
    scheduler.preempt(spot, high.task)
    RESUME_AFTER.setdefault(high.task, []).append(spot.task)
    # 高优任务绑定到同一节点并启动
    high.node = n.node; high.state = TaskState.BINDING.value
    high.bound_at = now_ms()
    db.upsert_task(high)
    try:
        _worker(n, '/start', json={'task': high.task, 'prompt': high.prompt,
                                   'task_type': high.task_type,
                                   'tokens_in': high.tokens_in})
        high.state = TaskState.RUNNING.value
        db.upsert_task(high)
    except Exception as e:
        # 原实现只 return False：高优任务卡在 binding、被抢占的 spot 永不恢复。
        # 这里显式回滚：撤销抢占登记，把 spot 恢复运行，高优任务退回排队。
        if high.task in RESUME_AFTER:
            RESUME_AFTER[high.task] = [x for x in RESUME_AFTER[high.task]
                                       if x != spot.task]
            if not RESUME_AFTER[high.task]:
                RESUME_AFTER.pop(high.task, None)
        try:
            _worker(n, '/resume', params={'task': spot.task})
            spot.state = TaskState.RUNNING.value
            db.upsert_task(spot)
            db.add_event('rollback',
                         f'{high.task} 启动失败，已恢复被抢占的 {spot.task}',
                         node=n.node, task=spot.task)
        except Exception as e2:                       # noqa: BLE001
            db.add_event('rollback',
                         f'{spot.task} 恢复失败：{str(e2)[:60]}',
                         node=n.node, task=spot.task)
        high.state = TaskState.QUEUED.value
        high.node = None
        high.attempts += 1
        db.upsert_task(high)
        return False, f'高优任务启动失败（{str(e)[:60]}），已回滚抢占'
    return True, f'{high.task} 抢占 {spot.task}（progress=' \
                 f"{ck.get('progress',0):.0%}）"


# ---------------- 主循环 ----------------
def _tick():
    detect_lost()
    reclaim_stuck_binding()
    launch_queued()
    poll_running()


async def run():
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception as e:   # 闭环不能因异常退出
            db.add_event('loop-error', f'harness 循环异常：{str(e)[:80]}')
        await asyncio.sleep(config.LOOP_INTERVAL_S)
