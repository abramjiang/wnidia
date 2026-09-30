# -*- coding: utf-8 -*-
"""Agent 决策策略（v5.1）：LLM 提议 → 确定性内核裁决 → 采纳 / 回落。

为什么这样分层（答辩核心话术）：
    原 scheduler.py 的「决策全部为确定性 Python，模型无法绕过」是一条**安全性质**，
    不是缺陷。本模块不推翻它，而是给它开一个**提议通道**：

        1. Agent（LLM）只在**候选集**里选目标节点——集外选择在源头就被拒；
        2. 内核用与正常调度**完全相同**的硬约束（准入/显存/时延/密级/主权/机密）
           复核提议，任一不过即 veto，写明理由；
        3. enforce 下通过才采纳；shadow 下只留痕不影响派发；
           任何异常（Agent 不可达 / 超时 / 坏 JSON）一律回落 scheduler.match。

    结果：模型可以参与决策，但无法越界——每次 veto 都有可解释理由，
    每次提议都有留痕（agent_decisions 表），一致率可量化（/admin/agent-policy）。

关键实现约束：
    - 控制面主循环（harness._tick，2s 一轮）里发起 LLM 调用，**必须短超时**
      （AGENT_PROPOSE_TIMEOUT 默认 6s），超时/拒绝连接即刻回落，绝不阻塞调度；
    - 本模块只读 scheduler 的公开/私有函数（同包内复用，不复制规则），
      保证「Agent 走的约束」与「内核走的约束」永远同源，不会出现两套口径。
"""
import threading
import time
from typing import List, Optional, Tuple

from . import config, db, httpcli, scheduler
from .models import TaskSpec

MODES = ('off', 'shadow', 'enforce')


def mode() -> str:
    m = (config.AGENT_POLICY or 'off').strip().lower()
    return m if m in MODES else 'off'


def enabled() -> bool:
    return mode() != 'off'


# ---------------------------------------------------------------- 提议获取
def _candidate_view(cands) -> List[dict]:
    """给 LLM 看的候选画像：只给决策相关字段，不给内部实现细节。"""
    return [{'node': n.node, 'tier': n.tier, 'role': n.role,
             'free_mem_gb': round(float(n.free_mem_gb or 0), 1),
             'util_pct': round(float(n.util or 0), 1),
             'latency_ms': round(float(getattr(n, 'latency_ms', 0) or 0), 1),
             'reputation': round(float(n.reputation or 1), 2),
             'kv_hit': round(float(n.kv_hit or 0), 2)} for n in cands]


def proposal_from_agent(t: TaskSpec, cands) -> Tuple[Optional[str], str, float]:
    """向 Agent 服务要一个结构化提议。返回 (node, source, latency_ms)。

    失败一律返回 (None, 'unavailable', 0)：调用方据此走回落路径。
    最近 veto 理由会拼进提示词（闭环回流，Agent 自我修正）。
    注意 retries=1：httpcli 默认 3 次重试是为幂等的 worker 调用设计的，
    对 LLM 调用重试只会放大主循环停顿，超时一次就应回落。
    """
    t0 = time.time()
    vetoes = db.recent_agent_vetoes(5)
    body = {
        'task': {'task_type': t.task_type, 'tokens_in': t.tokens_in,
                 'secret': t.secret, 'sla': t.sla,
                 'need_mem_gb': float(t.need_mem_gb or 0),
                 'latency_budget_ms': float(t.latency_budget_ms or 0)},
        'candidates': _candidate_view(cands),
        'recent_vetoes': vetoes,
    }
    try:
        r = httpcli.post(f'{config.AGENT_BASE}/v1/agent/propose', json=body,
                         headers={'Authorization': f'Bearer {config.API_TOKEN}'},
                         timeout=config.AGENT_PROPOSE_TIMEOUT, retries=1)
        j = r.json()
    except Exception:                       # noqa: BLE001  连接失败/超时/非 JSON
        return None, 'unavailable', round((time.time() - t0) * 1000, 1)
    node = (j or {}).get('target_node') or ''
    if node not in {c.node for c in cands}:
        # Agent 服务返回了候选集外的节点：视为无效提议（源头上已经约束过，
        # 这里再拦一次是防 Agent 服务被替换/降级后行为漂移）
        return None, 'invalid', round((time.time() - t0) * 1000, 1)
    return node, (j or {}).get('source') or 'llm', round((time.time() - t0) * 1000, 1)


# ---------------------------------------------------------------- 裁决
def _validate(t: TaskSpec, node: str, cands) -> str:
    """提议复核。返回 veto 理由（''=通过）。

    复用 _candidates 的产物做成员校验即可——候选集生成时已逐条执行
    准入/显存/主权/密级/时延/机密全部硬约束，成员资格即合规证明。
    """
    if not node:
        return 'empty_proposal'
    if node not in {c.node for c in cands}:
        return 'not_in_candidates'
    n = next(c for c in cands if c.node == node)
    if n.status != 'online':
        return 'node_offline'
    if n.cheat:
        return 'node_untrusted'
    return ''


def _propose_and_record(t: TaskSpec, cands, kernel_node: str, m: str):
    """提议 → 裁决 → 留痕（在后台线程中执行，不阻塞派发主循环）。

    m 是 decide() 调用时刻的档位快照：线程启动后若配置被热切换，
    本线程仍按原档位记录，避免 shadow 决策被误记成 enforce 采纳。
    任何异常（旧库未迁移/磁盘满/连接中断）只丢这条留痕，绝不影响派发。
    """
    try:
        node, source, lat = proposal_from_agent(t, cands)
        if node is None:
            if source in ('unavailable', 'invalid'):
                db.add_agent_decision(t.task, m, source, '', kernel_node,
                                      agreed=False, adopted=False,
                                      veto=f'agent_{source}', latency_ms=lat)
            return
        veto = _validate(t, node, cands)
        adopted = bool(m == 'enforce' and not veto)
        agreed = bool(kernel_node and node == kernel_node and not veto)
        db.add_agent_decision(t.task, m, source, node, kernel_node,
                              agreed=agreed, adopted=adopted, veto=veto,
                              latency_ms=lat)
        if veto:
            db.add_event('agent-veto',
                         f'{t.task} Agent 提议 {node} 被否（{veto}），'
                         f'回落内核选路（内核选 {kernel_node or "无"}）', task=t.task)
        elif adopted:
            db.add_event('agent-bind',
                         (f'{t.task} 采纳 Agent 提议 {node}（与内核一致）' if agreed
                          else f'{t.task} 采纳 Agent 提议 {node}'
                               f'（内核原选 {kernel_node}）'),
                         node=node, task=t.task)
    except Exception:                       # noqa: BLE001  留痕失败不扩散
        return


def decide(t: TaskSpec) -> Optional[str]:
    """派发前的 Agent 策略入口。返回 prefer_node（None=回落内核自选）。

    - off：直接 None（不产生任何留痕噪声）；
    - shadow：**后台线程**留痕，立即返回 None——主循环零停顿，派发不受
      LLM 时延影响（这是 shadow 与 enforce 的本质区别，不只是"不采纳"）；
    - enforce：同步提议+裁决（受 AGENT_PROPOSE_TIMEOUT 约束），
      通过才返回节点，否则回落 scheduler.match。
    """
    if not enabled():
        return None
    cands = scheduler._candidates(t)
    if not cands:
        return None                          # 无候选时内核自己会走拒绝路径
    kernel = scheduler.match(t)
    kernel_node = kernel.node if kernel else ''
    m = mode()                               # 档位快照：线程/后续逻辑统一口径
    if m == 'shadow':
        threading.Thread(target=_propose_and_record,
                         args=(t, cands, kernel_node, m), daemon=True).start()
        return None
    node, source, lat = proposal_from_agent(t, cands)
    if node is None:
        if source in ('unavailable', 'invalid'):
            try:
                db.add_agent_decision(t.task, m, source, '', kernel_node,
                                      agreed=False, adopted=False,
                                      veto=f'agent_{source}', latency_ms=lat)
            except Exception:               # noqa: BLE001  留痕失败不影响派发
                pass
        return None
    veto = _validate(t, node, cands)
    adopted = bool(m == 'enforce' and not veto)
    agreed = bool(kernel_node and node == kernel_node and not veto)
    try:
        db.add_agent_decision(t.task, m, source, node, kernel_node,
                              agreed=agreed, adopted=adopted, veto=veto,
                              latency_ms=lat)
        if veto:
            db.add_event('agent-veto',
                         f'{t.task} Agent 提议 {node} 被否（{veto}），'
                         f'回落内核选路（内核选 {kernel_node or "无"}）', task=t.task)
        elif adopted:
            db.add_event('agent-bind',
                         (f'{t.task} 采纳 Agent 提议 {node}（与内核一致）' if agreed
                          else f'{t.task} 采纳 Agent 提议 {node}'
                               f'（内核原选 {kernel_node}）'),
                         node=node, task=t.task)
    except Exception:                       # noqa: BLE001  度量表故障不否决采纳
        pass
    return node if adopted else None
