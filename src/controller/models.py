# -*- coding: utf-8 -*-
"""类型化领域模型：Harness 的“类型化输入/输出、显式对象状态”。"""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
import time


def now_ms() -> int:
    return int(time.time() * 1000)


# ============ 枚举 ============
class NodeTier(str, Enum):
    CLOUD = 'cloud'     # 中心云
    EDGE  = 'edge'      # 边缘
    HOME  = 'home'      # 端侧/家庭
    CPU   = 'cpu'       # CPU 弱节点


class NodeStatus(str, Enum):
    ONLINE  = 'online'
    LOST    = 'lost'
    DRAIN   = 'drain'


class SLA(str, Enum):
    S1 = 'sla-1'   # 实时/最高优
    S2 = 'sla-2'   # 在线交互
    S3 = 'sla-3'   # 离线/spot 可抢占


class Secret(str, Enum):
    L1 = 'L1'      # 公开
    L2 = 'L2'      # 内部
    L3 = 'L3'      # 机密
    L4 = 'L4'      # 绝密/强主权


class TaskState(str, Enum):
    QUEUED     = 'queued'
    BINDING    = 'binding'
    RUNNING    = 'running'
    PREEMPTED  = 'preempted'
    DONE       = 'done'
    FAILED     = 'failed'
    REJECTED   = 'rejected'


class Admission(str, Enum):
    OK             = 'ok'
    REJECT_QUOTA   = 'quota_exceeded'
    REJECT_SECRET  = 'secret_not_satisfiable'
    REJECT_CAP     = 'no_capacity'
    # 三维路由第三维的硬约束失败：把"时延预算不可满足"从"没机器"里分出来，
    # 否则看板上会被误读成算力不足，排查方向完全错（沙盒 B3 用例）。
    REJECT_LATENCY = 'latency_budget_unmet'


# ============ 节点画像 ============
@dataclass
class NodeProfile:
    node: str
    role: str = 'decode'                 # prefill / decode / cpu
    tier: str = NodeTier.EDGE.value
    mem_total_gb: float = 8.0
    mem_limit_gb: float = 8.0
    compute_pct: int = 100
    vllm_port: int = 8100
    trusted: bool = False
    sovereign_region: str = 'local'
    # 运行期
    status: str = NodeStatus.ONLINE.value
    util: float = 0.0                    # 0..100
    free_mem_gb: float = 8.0
    kv_hit: float = 0.0
    reputation: float = 1.0
    last_heartbeat: int = 0
    registered_at: int = field(default_factory=now_ms)
    gpu_name: str = 'mock-gpu'
    cheat: bool = False                  # 是否为作弊节点（演示注入）
    engine: str = 'mock'                 # 该节点实际绑定的推理引擎（引擎可插拔层）
    engine_healthy: Optional[bool] = None  # 引擎最近一次探活/调用结果
    degraded: bool = False               # 是否已从首选引擎降级
    # ---- v4：资源画像补维度（BP P7「能力画像打分分级」/ P13 L1）----
    bandwidth_mbps: float = 0.0          # 上行带宽（Mbps），家庭节点关键维度
    uptime_ratio: float = 1.0            # 在线率（0..1）
    stability: float = 1.0              # 稳定性评分（0..1，掉线/抖动后下调）
    latency_ms: float = 0.0              # 到控制面的往返时延（三维路由第三维）
    power_w: int = 0                     # 整机功耗（边缘箱选型与放置约束）
    form_factor: str = 'unknown'         # box / mini / server / vm / sim
    generation: str = ''                 # 代际标签（跨代际混部）
    # ---- v4：机密计算层级（BP P9；与数据密级 secret 完全独立）----
    cc_level: str = 'CC-L0'              # CC-L0..CC-L4，见 capabilities.CC_LEVELS
    qpu_units: int = 0                   # QPU 资源抽象（BP P18，沙盒模拟）
    # ---- v4：边缘断网自治（BP P13/P14）----
    offline: bool = False                # 是否处于断网自治状态
    offline_since: int = 0               # 进入断网的时刻
    pending_local: int = 0               # 本地队列待回放的任务数

    def to_dict(self):
        return asdict(self)

    @property
    def layer(self):
        """对外分层（云—边—端三层），见 capabilities.TIER_TO_LAYER。"""
        from . import capabilities
        return capabilities.public_layer(self.tier)


# ============ 任务规约 ============
@dataclass
class TaskSpec:
    task: str                            # 任务 id
    tenant: str = 'demo'
    prompt: str = ''
    task_type: str = 'chat'              # chat / heavy / embed / batch
    sla: str = SLA.S2.value
    secret: str = Secret.L1.value
    tokens_in: int = 128
    need_mem_gb: float = 1.0
    cost: int = 1
    sovereign_region: str = 'local'
    verify: bool = False                 # 是否要求多数决校验
    # 运行期
    state: str = TaskState.QUEUED.value
    node: Optional[str] = None
    progress: float = 0.0
    attempts: int = 0
    answer: str = ''
    error: str = ''
    created_at: int = field(default_factory=now_ms)
    bound_at: int = 0
    finished_at: int = 0
    # ---- v4：三维路由（BP P5：时延 / 成本 / 密级）----
    latency_budget_ms: float = 0.0       # >0 表示硬约束：时延超标节点不得入选
    # ---- v4：机密计算准入（BP P9）----
    cc_required: str = ''                # 为空则按 secret 推导（capabilities.required_cc）
    # ---- v4：计费口径（BP P10：token / 节点 / 席位）----
    billing_mode: str = 'token'          # token | node | seat
    project: str = ''
    department: str = ''
    # ---- v4：真实计量（BP P10「账单与算力一一对应」）----
    prompt_tokens: int = 0
    completion_tokens: int = 0
    metering_estimated: bool = False     # 计量是否来自估算而非引擎 usage
    engine: str = ''                     # 实际执行引擎（留痕用）
    # ---- v4：可复现（引擎层一致性）----
    exact_required: bool = False
    consistency_ok: Optional[bool] = None
    # ---- v5：推理数据留存策略（BP P13 决策③「推理数据留本地」）----
    # 默认不落库 prompt 明文，只落摘要与长度；明文只在控制面进程内存里，
    # 见 controller/prompt_guard.py。这两个字段让"确实没存明文"可被核验。
    prompt_digest: str = ''              # sha256(prompt)[:16]
    prompt_chars: int = 0                # prompt 长度（计量与统计用）

    def to_dict(self):
        return asdict(self)


@dataclass
class ScheduleResult:
    admitted: Admission
    node: Optional[str] = None
    reason: str = ''


@dataclass
class Event:
    ts: int
    kind: str                            # register / bind / preempt / lost / verify ...
    message: str
    node: str = ''
    task: str = ''

    def to_dict(self):
        return asdict(self)
