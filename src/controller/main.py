# -*- coding: utf-8 -*-
"""WNIDIA 控制面 API（:9000）：网关 + 内部注册/心跳 + 管理/注入 + v4 能力面。

v4 新增能力面（对应 BP 补课项）：
    /admin/capabilities  能力与口径总表（BP × 代码差距标注 §8 的机器可读版）
    /admin/metering      真实计量与分账（P10）
    /admin/settlement    机主贡献结算（P7）
    /admin/trust         机密计算层级与证明（P9）
    /admin/audit/*       审计哈希链与证据包（P9 CC-L3）
    /admin/gates         GO/NO-GO 门槛度量（P21）
    /admin/offline       边缘断网自治与批次回放（P13/P14）
    /admin/qpu           QPU 资源抽象（P18，模拟）
    /admin/fleet         具身机队（P18，仿真）
    /admin/time-slice    分时复用策略（P13 L2）
    /admin/subscriptions 订阅分档（P10）
    /portal              租户自助门户（P13 接入层）
    /v1/tenant/{t}/usage 租户用量与账单（P13）
"""
import asyncio
import os
import time
import uuid
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import (capabilities, compliance, config, db, demo, fleet, gates,
               harness, metering, offline, prompt_guard, qpu, registry,
               scheduler, settlement, trust)
from . import engines as _eng
from . import executor as _exec
from . import httpcli, jevclient
from .models import NodeProfile, SLA, Secret, TaskSpec, TaskState, now_ms

app = FastAPI(title='WNIDIA Controller', version='5.0.0')
_origins = [o.strip() for o in
            os.getenv('WNIDIA_CORS_ORIGINS', '').split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=_origins or ['http://127.0.0.1:8888',
                                               'http://localhost:8888'],
    allow_methods=['GET', 'POST'], allow_headers=['Authorization',
                                                  'Content-Type'])


# ---------------- 鉴权 ----------------
def check_token(authorization: Optional[str] = Header(default=None)):
    expect = f'Bearer {config.API_TOKEN}'
    if authorization != expect:
        raise HTTPException(status_code=401, detail='invalid token')
    return True


# ---------------- 内部：注册 / 心跳 ----------------
class RegisterReq(BaseModel):
    node: str; role: str = 'decode'; tier: str = 'edge'
    mem_total_gb: float = 8; mem_limit_gb: float = 8; compute_pct: int = 100
    vllm_port: int = 8100; trusted: bool = False
    sovereign_region: str = 'local'; gpu_name: str = 'mock-gpu'
    engine: str = 'mock'
    # v4 画像
    form_factor: str = 'unknown'; generation: str = ''
    cc_level: str = 'CC-L0'; bandwidth_mbps: float = 0
    power_w: int = 0


class HeartbeatReq(BaseModel):
    node: str; util: float = 0; free_mem_gb: float = 8; kv_hit: float = 0
    engine: Optional[str] = None
    engine_healthy: Optional[bool] = None
    degraded: Optional[bool] = None
    latency_ms: Optional[float] = None
    uptime_ratio: Optional[float] = None
    stability: Optional[float] = None
    offline: Optional[bool] = None
    pending_local: Optional[int] = None


@app.post('/internal/register')
def register(req: RegisterReq, _=Depends(check_token)):
    p = NodeProfile(**req.dict())
    registry.register(p)
    trust.seal('node-register', p.node, f'{p.tier}|{p.cc_level}|{p.form_factor}')
    return {'ok': True, 'node': p.node}


@app.post('/internal/heartbeat')
def heartbeat(req: HeartbeatReq, _=Depends(check_token)):
    ok = registry.heartbeat(
        req.node, req.util, req.free_mem_gb, req.kv_hit,
        engine=req.engine, engine_healthy=req.engine_healthy,
        degraded=req.degraded, latency_ms=req.latency_ms,
        uptime_ratio=req.uptime_ratio, stability=req.stability,
        offline=req.offline, pending_local=req.pending_local)
    if not ok:
        raise HTTPException(status_code=404, detail='node not registered')
    return {'ok': True}


class OfflineBatchReq(BaseModel):
    batch_id: str; node: str; tasks: int = 0; tokens: int = 0
    node_seconds: float = 0.0; note: str = ''


@app.post('/internal/offline/batch')
def offline_batch(req: OfflineBatchReq, _=Depends(check_token)):
    """边缘断网批次回放（幂等：同一 batch_id 重复提交只记一次）。"""
    return offline.ingest_batch(req.dict())


# ---------------- 网关：OpenAI 兼容 ----------------
class ChatMessage(BaseModel):
    role: str; content: str


class ChatReq(BaseModel):
    model: Optional[str] = 'stepfun'
    messages: List[ChatMessage]
    sla: Optional[str] = None
    secret: Optional[str] = None
    task_type: Optional[str] = None
    verify: Optional[bool] = None
    tenant: Optional[str] = 'demo'
    # v4：三维路由与计费口径
    latency_budget_ms: Optional[float] = None
    cc_required: Optional[str] = None
    billing_mode: Optional[str] = None
    project: Optional[str] = None
    department: Optional[str] = None
    prefer_exact: Optional[bool] = None


def _uid(prefix):
    return f'{prefix}-{now_ms()}-{uuid.uuid4().hex[:6]}'


def _derive(req: ChatReq) -> TaskSpec:
    prompt = '\n'.join(m.content for m in req.messages)
    tokens_in = max(32, len(prompt) // 2)
    task_type = req.task_type or ('heavy' if tokens_in > 1500 else 'chat')
    secret = req.secret or ('L2' if any(
        k in prompt for k in ('合同', '客户', '内部')) else 'L1')
    sla = req.sla or (SLA.S2.value if task_type == 'chat' else SLA.S3.value)
    need_mem = round(0.8 + tokens_in / 2000.0, 2)
    cost = max(1, tokens_in // 128)
    exact = bool(req.prefer_exact) or (
        capabilities.SECRET_RANK.get(secret, 1) >= 3)
    return TaskSpec(
        task=_uid('t'), tenant=req.tenant, prompt=prompt,
        task_type=task_type, sla=sla, secret=secret, tokens_in=tokens_in,
        need_mem_gb=need_mem, cost=cost, verify=bool(req.verify),
        latency_budget_ms=float(req.latency_budget_ms or 0),
        cc_required=req.cc_required or '',
        billing_mode=(req.billing_mode or metering.default_mode()),
        project=req.project or '', department=req.department or '',
        exact_required=exact)


@app.post('/v1/chat/completions', dependencies=[Depends(check_token)])
async def chat_completions(req: ChatReq):
    t = _derive(req)
    # v5：空输入属于**输入错误**，必须在入口按输入错误拒绝。
    # 不校验的话，空 prompt 的任务会先进库，被调度循环判成 prompt_empty 失败，
    # 网关再把 409 回给调用方 —— 状态码语义错了（409 是"冲突"，
    # 不是"你给的参数不对"），而且会在任务列表里留下一条 FAILED 垃圾记录。
    if not (t.prompt or '').strip():
        raise HTTPException(
            status_code=422,
            detail='messages 为空或内容全为空白：没有可执行内容')
    if config.JEV_GUARDRAIL:
        scr = jevclient.screen_prompt(t.prompt)
        if scr is not None:
            if scr['verdict'] == 'block':
                jevclient.C['blocks'] += 1
                db.add_event('guardrail',
                             f'入口护栏拦截（{"、".join(scr["reasons"])}，'
                             f'risk={scr["risk"]}）')
                raise HTTPException(status_code=400,
                                    detail=f'blocked_by_guardrail:{scr["reasons"]}')
            if scr['verdict'] == 'review':
                db.add_event('guardrail',
                             f'入口护栏标记待复核（risk={scr["risk"]}）', task=t.task)
    db.upsert_task(t)
    db.ensure_quota(t.tenant, config.DEFAULT_QUOTA, t.billing_mode)
    trust.seal('task-submit', t.task,
               f'{t.tenant}|{t.secret}|{t.sla}|{t.billing_mode}')
    deadline = time.time() + config.WORKER_CALL_TIMEOUT
    while time.time() < deadline:
        cur = db.get_task(t.task)
        if cur.state in (TaskState.DONE.value, TaskState.REJECTED.value,
                         TaskState.FAILED.value):
            if cur.state == TaskState.DONE.value:
                return {
                    'id': cur.task, 'object': 'chat.completion',
                    'model': req.model,
                    'choices': [{'index': 0,
                                 'message': {'role': 'assistant',
                                             'content': cur.answer},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': cur.prompt_tokens,
                              'completion_tokens': cur.completion_tokens,
                              'total_tokens': cur.prompt_tokens +
                                              cur.completion_tokens},
                    'wnidia': {'node': cur.node, 'engine': cur.engine,
                               'billing_mode': cur.billing_mode,
                               'metering_estimated': cur.metering_estimated,
                               'consistency_ok': cur.consistency_ok},
                }
            raise HTTPException(status_code=409, detail=f'{cur.state}:{cur.error}')
        await asyncio.sleep(0.4)
    raise HTTPException(status_code=504, detail='scheduling timeout')


# ---------------- 管理：总览 ----------------
@app.get('/admin/state', dependencies=[Depends(check_token)])
def state(reveal_prompt: bool = False):
    """集群总览。

    v5：任务一律过脱敏闸 —— prompt 默认只给预览 + 摘要，不给全文
    （字段级脱敏的落点集中在这里，避免漏掉某个视图）。
    `?reveal_prompt=1` 只有在 `WNIDIA_STORE_PROMPT=1` 且
    `WNIDIA_REVEAL_PROMPT=1` 时才真的返回全文，否则仍是预览。
    """
    return {
        'nodes': [dict(n.to_dict(), layer=n.layer) for n in db.all_nodes()],
        'tasks': [prompt_guard.redact_task(t.to_dict(),
                                           reveal=bool(reveal_prompt))
                  for t in db.all_tasks()],
        'events': [e.to_dict() for e in db.recent_events(60)],
        'ledger': db.ledger_rows(60),
        'jev': jevclient.status(),
        'engine': {
            'active': _eng.resolve(),
            'catalog': list(_eng.CATALOG),
            'node_engines': {n.node: {'engine': n.engine,
                                      'healthy': n.engine_healthy,
                                      'degraded': n.degraded}
                             for n in db.all_nodes()},
        },
        'layers': capabilities.layer_summary(),
    }


@app.get('/admin/agent-policy', dependencies=[Depends(check_token)])
def agent_policy_view():
    """Agent 决策策略度量：模式、一致率、采纳率、veto 分布、最近留痕。

    答辩口径：Agent 提议 N 次、内核采纳 M 次、每次 veto 都有可解释理由——
    「LLM 提议、内核裁决：模型可以参与决策，但无法越界」的数据支撑。
    """
    from . import agent_policy as _ap
    return {'mode': _ap.mode(),
            'stats': db.agent_policy_stats(),
            'recent': db.agent_decisions(30)}


# ---------------- 场景编排器（P1：BP 对齐的可演示剧本） ----------------
@app.get('/admin/demo/scenes', dependencies=[Depends(check_token)])
def demo_scenes():
    """场景清单（供门户页渲染卡片）：每个场景的步骤标题与 BP 依据。"""
    return {'scenes': demo.scenes()}


@app.post('/admin/demo/run', dependencies=[Depends(check_token)])
def demo_run(scene: str = 'edge'):
    """启动一个场景剧本（后台线程逐步执行，每步真实留痕）。"""
    r = demo.run(scene)
    if not r.get('ok'):
        raise HTTPException(status_code=400, detail=r.get('error'))
    return r


@app.get('/admin/demo/status', dependencies=[Depends(check_token)])
def demo_status():
    """当前场景进度与每步的真实测量结果。"""
    return demo.status()


@app.post('/admin/demo/stop', dependencies=[Depends(check_token)])
def demo_stop(all: bool = Query(default=False)):
    """停止当前场景；all=true 时为总开关（一并终止演示任务、停止流量进程）。"""
    return demo.stop(all_=all)


@app.post('/admin/demo/pause', dependencies=[Depends(check_token)])
def demo_pause():
    """暂停当前场景（当前步骤执行完后停在下一步之前）。"""
    return demo.pause()


@app.post('/admin/demo/resume', dependencies=[Depends(check_token)])
def demo_resume():
    """恢复暂停的场景。"""
    return demo.resume()


@app.post('/admin/demo/clear', dependencies=[Depends(check_token)])
def demo_clear():
    """删除演示任务历史记录（不碰真实业务任务）。"""
    return demo.clear_demo_tasks()


@app.post('/admin/bench/best', dependencies=[Depends(check_token)])
def bench_best(tasks: int = Query(default=6)):
    """一键性能实测：并发轻任务，按节点统计时延与吞吐。"""
    return demo.bench_best(tasks=tasks)


@app.get('/admin/feedback', dependencies=[Depends(check_token)])
def engine_feedback():
    """调度引擎（harness）运行数据 + 语义评审（JEV）数据，供前端两栏展示。"""
    return demo.engine_feedback()


@app.get('/admin/jev/report', dependencies=[Depends(check_token)])
def jev_report(limit: int = Query(default=12)):
    """JEV 日志报告：逐条评测近期任务的执行状况与收益实现（真实留痕）。"""
    return demo.jev_report(limit=limit)


@app.post('/admin/traffic/start', dependencies=[Depends(check_token)])
def traffic_start(concurrency: int = Query(default=3),
                  duration: float = Query(default=120.0),
                  max_tokens: int = Query(default=96)):
    """启动流量发生器（真实任务注入调度闭环，让看板呈现真实调度过程）。"""
    return demo.traffic_start(concurrency=concurrency, duration=duration,
                              max_tokens=max_tokens)


@app.post('/admin/traffic/stop', dependencies=[Depends(check_token)])
def traffic_stop():
    """停止流量发生器。"""
    return demo.traffic_stop()


@app.get('/admin/traffic/status', dependencies=[Depends(check_token)])
def traffic_status():
    """流量发生器运行状态。"""
    return demo.traffic_status()


@app.get('/admin/engines', dependencies=[Depends(check_token)])
def engines_state(probe: bool = True, timeout: float = None):
    _, decision = _eng.select(
        secret='L3' if os.getenv('WNIDIA_ENGINE_EXACT_DEMO') else 'L1',
        do_probe=bool(probe), timeout=timeout or config.ENGINE_TIMEOUT)
    return {'active': _eng.resolve(), 'catalog': _eng.catalog(),
            'live': _eng.probe_all(timeout=timeout or config.ENGINE_TIMEOUT)
            if probe else {},
            'decision': decision}


@app.get('/admin/compliance', dependencies=[Depends(check_token)])
def compliance_state():
    pub = int(config.API_PORT) in compliance.PUBLIC_PORTS
    return {
        'public_ports': list(compliance.PUBLIC_PORTS),
        'public_ip': config.PUBLIC_IP,
        'node_num': config.NODE_NUM or '(未设置 NODE_NUM)',
        'public_urls': {'dashboard': compliance.public_port_of(8888),
                        'api': compliance.public_port_of(9000),
                        'ssh': compliance.public_port_of(22)},
        'token_masked': compliance.mask(config.API_TOKEN),
        'dash_pass_masked': compliance.mask(config.DASH_PASS),
        'token_strength_ok': not compliance.is_weak(config.API_TOKEN),
        'dash_pass_strength_ok': not compliance.is_weak(config.DASH_PASS),
        'api_on_public_port': pub,
        'probe_allowlist': sorted(compliance.allowlist()),
        # v5：推理数据留存策略（可投屏自证"prompt 确实没落明文"）
        'prompt_policy': prompt_guard.status(),
        'rule_map': 'docs/COMPLIANCE.md',
    }


# ---------------- 管理：口径与能力 ----------------
@app.get('/admin/capabilities', dependencies=[Depends(check_token)])
def capabilities_state():
    """能力与口径总表：答辩时可直接投屏，避免"把 decision_only 说成已支持"。"""
    return {
        'board': capabilities.capability_board(),
        'terminology': capabilities.terminology_map(),
        'layers': capabilities.layer_summary(),
        'subscriptions': capabilities.subscription_catalog(),
        'note': ('凡 status=decision_only / simulated 的能力，对外表述见 outward 字段，'
                 '不得说成"已支持"。'),
    }


@app.get('/admin/subscriptions', dependencies=[Depends(check_token)])
def subscriptions_state():
    return {'tiers': capabilities.subscription_catalog(),
            'note': '档位为定价与能力包的声明，非强制计费闸门（沙盒展示用）。'}


@app.get('/admin/time-slice', dependencies=[Depends(check_token)])
def time_slice_state(window_minutes: int = 60, slice_minutes: int = 15,
                     tenants: str = 'demo'):
    return scheduler.time_slice_plan(
        window_minutes=window_minutes, slice_minutes=slice_minutes,
        tenants=[x.strip() for x in tenants.split(',') if x.strip()])


# ---------------- 管理：计量 / 结算 ----------------
@app.get('/admin/metering', dependencies=[Depends(check_token)])
def metering_state(limit: int = 2000):
    rows = db.ledger_rows(limit)
    return {'summary': metering.summarize(rows),
            'quality': metering.metering_quality(rows),
            'pricing': metering.pricing_catalog(),
            'billing_modes': list(metering.BILLING_MODES)}


@app.post('/admin/settlement', dependencies=[Depends(check_token)])
def settlement_run(period: Optional[str] = None, window_s: Optional[float] = None,
                   persist: bool = True):
    if persist:
        return settlement.settle(period=period, window_s=window_s)
    return settlement.compute(period=period, window_s=window_s)


@app.get('/admin/settlement', dependencies=[Depends(check_token)])
def settlement_state(limit: int = 200):
    return settlement.status(limit)


# ---------------- 管理：可信与审计 ----------------
@app.get('/admin/trust', dependencies=[Depends(check_token)])
def trust_state():
    return trust.status()


@app.post('/admin/trust/prove', dependencies=[Depends(check_token)])
def trust_prove(node: str, cc_level: Optional[str] = None,
                ttl_s: Optional[int] = None):
    n = db.get_node(node)
    if not n:
        raise HTTPException(404, 'no node')
    if cc_level:
        n.cc_level = cc_level
        db.upsert_node(n)
    res = trust.prove(n, cc_level=cc_level, ttl_s=ttl_s)
    return res


@app.post('/admin/trust/verify', dependencies=[Depends(check_token)])
def trust_verify(node: str, cc_level: Optional[str] = None):
    return trust.verify_attestation(node=node, cc_level=cc_level)


@app.get('/admin/audit/verify', dependencies=[Depends(check_token)])
def audit_verify(limit: int = 5000):
    return trust.verify_chain(limit)


@app.post('/admin/audit/export', dependencies=[Depends(check_token)])
def audit_export(since_ms: int = 0, include_prompts: bool = False):
    return trust.export_evidence_pack(since_ms=since_ms,
                                      include_prompts=include_prompts)


# ---------------- 管理：门槛度量 ----------------
@app.get('/admin/gates', dependencies=[Depends(check_token)])
def gates_state(evaluate: bool = True, window_s: Optional[float] = None):
    if evaluate:
        return gates.evaluate(window_s=window_s)
    return gates.status()


# ---------------- 管理：边缘断网自治 ----------------
@app.get('/admin/offline', dependencies=[Depends(check_token)])
def offline_state():
    return offline.status()


@app.post('/admin/offline/simulate', dependencies=[Depends(check_token)])
def offline_simulate(node: str, seconds: float = 120, tasks: int = 3,
                     tokens: int = 1500):
    """沙盒：伪造一次断网（不真实切断网络），用于验证自治链路。"""
    return offline.simulate_offline(node, offline_seconds=seconds,
                                    tasks=tasks, tokens=tokens)


@app.post('/admin/offline/recover', dependencies=[Depends(check_token)])
def offline_recover(node: str, tasks: int = 3, tokens: int = 1500,
                    seconds: float = 120):
    """沙盒：恢复联网并回放批次。"""
    batch = offline.build_batch(node, tasks=tasks, tokens=tokens,
                                node_seconds=seconds, note='沙盒恢复批次')
    return offline.recover(node, batch=batch)


# ---------------- 管理：QPU ----------------
@app.get('/admin/qpu', dependencies=[Depends(check_token)])
def qpu_state():
    return {'status': qpu.status(), 'pqc': qpu.pqc_status()}


class QpuReq(BaseModel):
    job: Optional[str] = None
    circuit: List[dict] = []
    qubits: int = 2
    shots: int = 1024
    backend: str = qpu.SIM_BACKEND
    seed: int = 1234


@app.post('/admin/qpu/submit', dependencies=[Depends(check_token)])
def qpu_submit(req: QpuReq):
    job = req.job or _uid('q')
    return qpu.submit(job, req.circuit, qubits=req.qubits, shots=req.shots,
                      backend=req.backend, seed=req.seed)


# ---------------- 管理：具身机队 ----------------
@app.get('/admin/fleet', dependencies=[Depends(check_token)])
def fleet_state():
    return fleet.status()


class RobotReq(BaseModel):
    robot: str; fleet: str = 'fleet-a'; model_name: str = 'edge-brain'
    model_version: str = 'v1'; battery: float = 100.0
    region: str = 'local'; status: str = 'online'


@app.post('/admin/fleet/register', dependencies=[Depends(check_token)])
def fleet_register(req: RobotReq):
    return {'ok': True, 'robot': fleet.register_robot(**req.dict())}


class RolloutReq(BaseModel):
    model_name: str = 'edge-brain'; version: str = 'v2'
    rollout_pct: int = 50; dry_run: bool = False
    plan_stages: bool = False


@app.post('/admin/fleet/rollout', dependencies=[Depends(check_token)])
def fleet_rollout(req: RolloutReq):
    if req.plan_stages:
        return fleet.plan_rollout(req.model_name, req.version)
    return fleet.rollout_now(req.model_name, req.version, req.rollout_pct,
                             dry_run=req.dry_run)


class DispatchReq(BaseModel):
    tasks: List[dict] = []
    fleet: Optional[str] = None
    min_battery: float = 15.0


@app.post('/admin/fleet/dispatch', dependencies=[Depends(check_token)])
def fleet_dispatch(req: DispatchReq):
    return fleet.dispatch_tasks(req.tasks, fleet=req.fleet,
                                min_battery=req.min_battery)


class UplinkReq(BaseModel):
    robot: str; payload: str = ''


@app.post('/admin/fleet/uplink', dependencies=[Depends(check_token)])
def fleet_uplink(req: UplinkReq):
    return fleet.ingest_uplink(req.robot, req.payload)


# ---------------- 管理：演示注入（保留） ----------------
@app.post('/admin/reset', dependencies=[Depends(check_token)])
def reset():
    db.reset_db(); return {'ok': True}


class QueueTaskSpec(BaseModel):
    """批量入队的单条任务规格。

    BUG-V4-16：原实现用 `tasks: List[dict]` 直接 `int()/float()` 强转用户输入，
    传 `need_mem_gb="abc"` 会抛 ValueError → **HTTP 500**。
    收紧成显式类型后，类型错误由框架统一转成 422，不再 500；
    取值范围的兜底见 `_sanitize_queue_spec()`。
    """
    sla: Optional[str] = None
    secret: Optional[str] = None
    tenant: Optional[str] = None
    prompt: Optional[str] = None
    task_type: Optional[str] = None
    tokens_in: Optional[int] = None
    need_mem_gb: Optional[float] = None
    cost: Optional[int] = None


class QueueInjectReq(BaseModel):
    tasks: List[QueueTaskSpec] = []


VALID_SLAS = {SLA.S1.value, SLA.S2.value, SLA.S3.value}
VALID_TASK_TYPES = {'chat', 'heavy', 'batch'}


def _sanitize_queue_spec(spec: QueueTaskSpec):
    """把越界/非法取值收敛到安全默认，并返回被改写的字段名（便于回答"我没照做"）。

    原则：**演示注入端点不因为脏输入报错，但也不照抄脏输入** ——
    非法 sla 退回 sla-2、非法密级退回 L1、数值做区间夹取。
    """
    notes = []
    sla = (spec.sla or SLA.S2.value).strip()
    if sla not in VALID_SLAS:
        notes.append(f'sla:{sla}→{SLA.S2.value}')
        sla = SLA.S2.value
    secret = (spec.secret or Secret.L1.value).strip()
    if secret not in capabilities.SECRET_LEVELS:
        notes.append(f'secret:{secret}→{Secret.L1.value}')
        secret = Secret.L1.value
    ttype = (spec.task_type or 'chat').strip()
    if ttype not in VALID_TASK_TYPES:
        notes.append(f'task_type:{ttype}→chat')
        ttype = 'chat'
    tokens_in = int(min(max(spec.tokens_in or 64, 1), 10_000_000))
    need_mem = float(min(max(spec.need_mem_gb or 1.0, 0.1), 4096.0))
    cost = int(min(max(spec.cost or 1, 1), 1_000_000))
    return {'sla': sla, 'secret': secret, 'task_type': ttype,
            'tokens_in': tokens_in, 'need_mem_gb': need_mem, 'cost': cost,
            'tenant': (spec.tenant or 'demo-q')[:64],
            'prompt': (spec.prompt or '排队验证任务')[:2000]}, notes


@app.post('/admin/inject/queue', dependencies=[Depends(check_token)])
def inject_queue(req: QueueInjectReq):
    """演示/验证注入：把若干任务**同批**直接置为 queued。

    为什么需要它：`/v1/chat/completions` 是同步网关（提交后阻塞直到完成），
    因此无法通过它让两个任务同时排队 —— SLA 优先级排队也就无从观察。
    本端点一次请求内批量落库（单锁单事务），保证它们进入同一个调度节拍，
    出队顺序才由 sla 优先级唯一决定。
    """
    made, sanitized = [], []
    for i, spec in enumerate(req.tasks or []):
        s, notes = _sanitize_queue_spec(spec)
        if notes:
            sanitized.append({'index': i, 'changes': notes})
        made.append(TaskSpec(
            task=_uid('q'), tenant=s['tenant'], prompt=s['prompt'],
            task_type=s['task_type'], sla=s['sla'], secret=s['secret'],
            tokens_in=s['tokens_in'], need_mem_gb=s['need_mem_gb'],
            cost=s['cost'], billing_mode=metering.default_mode()))
    db.upsert_tasks(made)
    for t in made:
        db.ensure_quota(t.tenant, config.DEFAULT_QUOTA, t.billing_mode)
        trust.seal('task-submit', t.task,
                   f'{t.tenant}|{t.secret}|{t.sla}|inject-queue')
    return {'ok': True, 'count': len(made),
            'injected': [t.task for t in made],
            'sanitized': sanitized,
            'note': '按传入顺序创建；出队顺序由 SLA 优先级决定（sla-1 最优）。'}


@app.post('/admin/inject/preempt', dependencies=[Depends(check_token)])
def inject_preempt():
    high = TaskSpec(task=_uid('hi'), sla=SLA.S1.value, secret=Secret.L1.value,
                    prompt='紧急实时任务：立即处理', task_type='chat',
                    tokens_in=64, need_mem_gb=1.0, cost=1,
                    billing_mode=metering.default_mode())
    db.upsert_task(high)
    ok, msg = harness.inject_preemption(high)
    return {'ok': ok, 'message': msg}


@app.post('/admin/node/lost', dependencies=[Depends(check_token)])
def force_lost(node: str):
    n = registry.mark_status(node, 'lost')
    if not n:
        raise HTTPException(404, 'no node')
    db.add_event('lost', f'{node} 被手动置为离线（演示注入）', node=node)
    trust.seal('node-lost', node, str(now_ms()))
    for t in db.all_tasks():
        if t.node == node and t.state in (TaskState.RUNNING.value,
                                         TaskState.BINDING.value):
            t.state = TaskState.QUEUED.value; t.node = None; t.attempts += 1
            db.upsert_task(t)
            db.add_event('redispatch', f'{t.task} 因节点掉线重新排队', task=t.task)
    return {'ok': True}


@app.post('/admin/node/cheat', dependencies=[Depends(check_token)])
def toggle_cheat(node: str, on: bool = True):
    n = db.get_node(node)
    if not n:
        raise HTTPException(404, 'no node')
    registry.set_cheat(node, on)
    try:
        httpcli.post(f'{_exec.worker_base(n)}/set_cheat', params={'on': on},
                     timeout=10)
    except Exception:      # noqa: BLE001
        pass
    db.add_event('cheat', f'{node} 作弊开关={on}', node=node)
    return {'ok': True}


# ---------------- 租户侧（接入层 · 只读自助） ----------------
@app.get('/v1/tenant/{tenant}/usage', dependencies=[Depends(check_token)])
def tenant_usage(tenant: str, limit: int = 500, reveal_prompt: bool = False):
    rows = db.ledger_rows(limit, tenant=tenant)
    return {'tenant': tenant, 'summary': metering.summarize(rows),
            'quality': metering.metering_quality(rows),
            'quota': db.get_quota(tenant),
            # 租户自助视图同样过脱敏闸（对外出口不能有第二个口径）
            'tasks': [prompt_guard.redact_task(t.to_dict(),
                                              reveal=bool(reveal_prompt))
                      for t in db.all_tasks() if t.tenant == tenant][-50:]}


PORTAL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>WNIDIA 客户运维门户</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#f7f8fa;color:#1f2328;
font:14px/1.6 -apple-system,"PingFang SC",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:20px}
h1{font-size:18px;margin:0 0 4px}.sub{color:#6b7280;font-size:12px;margin-bottom:16px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px;margin-bottom:12px}
label{font-size:12px;color:#6b7280}input{border:1px solid #e3e6ea;border-radius:8px;padding:7px;font-size:13px;width:100%}
button{border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:8px;padding:8px 14px;cursor:pointer;margin-top:8px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{padding:6px 8px;border-bottom:1px solid #e3e6ea;text-align:left}
th{color:#6b7280;font-weight:400;font-size:12px}
.b{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;border:1px solid}
.b-ok{background:#eaf3de;color:#3b6d11;border-color:#97c459}
.b-no{background:#fcebeb;color:#a32d2d;border-color:#f09595}
.b-na{background:#f1efe8;color:#5f5e5a;border-color:#b4b2a9}
.mut{color:#888780;font-size:12px}
</style></head><body><div class="wrap">
<h1>WNIDIA 客户运维门户</h1>
<div class="sub">只读自助视图：用量 / 账单 / 门槛 / 结算。所有请求需 Bearer Token。</div>
<div class="card"><label>访问 Token</label><input id="tok" placeholder="Bearer Token">
<label style="margin-top:8px;display:block">租户</label><input id="tenant" value="demo">
<button onclick="load()">加载</button></div>
<div id="out"></div>
</div><script>
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
 return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function badge(v){var c=v===true?'b-ok':(v===false?'b-no':'b-na');
 return '<span class="b '+c+'">'+esc(v===true?'达标':(v===false?'未达标':'样本不足'))+'</span>';}
async function api(p){var t=document.getElementById('tok').value.trim();
 var r=await fetch(p,{headers:{'Authorization':'Bearer '+t}});
 if(!r.ok) throw new Error(p+' → HTTP '+r.status); return r.json();}
function table(rows,cols){var h='<table><tr>'+cols.map(function(c){return '<th>'+esc(c[0])+'</th>';}).join('')+'</tr>';
 rows.forEach(function(r){h+='<tr>'+cols.map(function(c){return '<td>'+c[1](r)+'</td>';}).join('')+'</tr>';});
 return h+'</table>';}
async function load(){
 var t=document.getElementById('tenant').value.trim()||'demo';
 var out=document.getElementById('out'); out.innerHTML='<div class="card">加载中…</div>';
 try{
  var u=await api('/v1/tenant/'+encodeURIComponent(t)+'/usage');
  var g=await api('/admin/gates');
  var s=await api('/admin/settlement');
  var html='';
  var tot=u.summary.total;
  html+='<div class="card"><b>用量与账单（'+esc(t)+'）</b>'
   +'<div class="mut">条目 '+tot.entries+' · tokens '+tot.tokens
   +' · 节点小时 '+(tot.node_hours||0)+' · 金额 ￥'+(tot.amount_cny||0)
   +' · 估算条目 '+tot.estimated_entries+'</div>'
   +'<div class="mut">计量质量：真实 '+(u.quality.real||0)+' / 估算 '+(u.quality.estimated||0)
   +'（真实占比 '+(u.quality.real_ratio==null?'—':u.quality.real_ratio)+'）</div>'
   +table(u.summary.by_billing_mode, [['口径',function(r){return esc(r.key);}],
     ['条目',function(r){return r.entries;}],['金额',function(r){return '￥'+r.amount_cny;}]])
   +'</div>';
  html+='<div class="card"><b>GO / NO-GO 门槛</b><div class="mut">总体判定：'
   +esc(g.overall)+'</div>'
   +table(g.gates,[['门槛',function(r){return esc(r.label);}],
     ['实测',function(r){return r.value==null?'—':r.value;}],
     ['目标',function(r){return r.comparator+' '+r.target;}],
     ['样本',function(r){return r.sample;}],
     ['判定',function(r){return badge(r.passed);}]])+'</div>';
  html+='<div class="card"><b>机主结算（规划比例）</b><div class="mut">机主分成 '
   +esc(s.owner_share_ratio)+' / 平台抽佣 '+(1-s.owner_share_ratio).toFixed(2)
   +' · 记录 '+s.records+' 条</div>'
   +'<div class="mut">'+esc(s.note)+'</div></div>';
  out.innerHTML=html;
 }catch(e){ out.innerHTML='<div class="card" style="color:#dc2626">'+esc(e.message)+'</div>'; }
}
</script></body></html>"""


@app.get('/portal', response_class=HTMLResponse)
def portal():
    """客户运维门户（只读）。页面本身不含数据，数据全部走 Bearer 鉴权接口。"""
    return HTMLResponse(PORTAL)


@app.get('/healthz')
def healthz():
    return {'ok': True, 'version': '5.0.0'}


@app.on_event('startup')
async def _start():
    exposed = not compliance.is_loopback(config.HOST)
    report = compliance.preflight(
        binds=[('API', config.HOST, config.API_PORT)],
        secrets=[('WNIDIA_TOKEN', config.API_TOKEN, exposed)],
        strict=config.COMPLIANCE_STRICT)
    for n in report['notes']:
        print(f'[compliance] {n}', flush=True)
    db.add_event('compliance',
                 f"启动自检通过（token={compliance.mask(config.API_TOKEN)}）")
    # v5：推理数据留存策略（BP P13 决策③「推理数据留本地」）
    pol = prompt_guard.status()
    if pol['store_plaintext']:
        print('[privacy] 警告：WNIDIA_STORE_PROMPT=1，prompt 将写入明文库，'
              '仅限本地复盘；对外接口仍按 WNIDIA_REVEAL_PROMPT 脱敏。',
              flush=True)
        db.add_event('privacy',
                     'prompt 明文落库已开启（STORE_PROMPT=1，本地复盘用途）')
    else:
        # 老库升级上来时，历史 prompt 是明文写进去的：只改新写入路径不会让
        # 旧数据消失，因此启动时主动抹一次（可用 WNIDIA_SCRUB_LEGACY=0 关闭）。
        n_sc = db.scrub_plaintext_prompts() if config.SCRUB_LEGACY_PROMPTS else 0
        print(f'[privacy] prompt 不落库明文；内存副本随任务终态释放。'
              f'历史明文已清理 {n_sc} 条。', flush=True)
        db.add_event('privacy',
                     f'prompt 不落库明文（STORE_PROMPT=0）；'
                     f'历史明文清理 {n_sc} 条；对外脱敏 '
                     f'reveal={pol["reveal_plaintext"]}')
    trust.seal('controller-start', 'boot',
               f'version=5.0.0 host={config.HOST} '
               f'store_prompt={int(pol["store_plaintext"])}')
    asyncio.create_task(harness.run())
