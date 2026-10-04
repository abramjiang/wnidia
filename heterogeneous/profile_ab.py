# -*- coding: utf-8 -*-
"""M3 · A/B 路由验证（升级方案 ② 的收益量化）

目的：把"异构编排有价值"从叙事变成**可测量的数字**。

⚠️ 诚实边界（重要）：
  - **真实 P50/P95 时延必须真实执行任务才能测得**，本模块不执行推理；
  - 本模块评估的是**路由决策质量**（是否选中瓶颈最匹配的设备）
    与**估算成本**（由 profile_metering 计算）；
  - 真实时延指标请在具备实测条件时，用本模块的 A/B 分组去驱动真实执行后补齐。

对照组 A：朴素基线（容量满足即可 → 选剩余容量最大的节点）
实验组 B：画像路由（profile_bonus 瓶颈匹配）
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

try:
    from .device_profile import DeviceProfile, DeviceRegistry
    from .profile_routing import TaskFeature, bottleneck_of, profile_bonus
    from .profile_metering import meter
except ImportError:
    from device_profile import DeviceProfile, DeviceRegistry
    from profile_routing import TaskFeature, bottleneck_of, profile_bonus
    from profile_metering import meter


@dataclass
class ABResult:
    rounds: int = 0
    a_hit_rate: float = 0.0          # 基线：选中理想设备的比例
    b_hit_rate: float = 0.0          # 画像路由：选中理想设备的比例
    a_cost_cny: float = 0.0          # 估算成本
    b_cost_cny: float = 0.0
    delta_hit_pp: float = 0.0        # 命中率提升（百分点）
    delta_cost_pct: float = 0.0      # 成本变化百分比（负=更省）
    detail: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


def _ideal_node(f: TaskFeature, reg: DeviceRegistry) -> Optional[str]:
    """解析出"理论上最匹配"的设备（按瓶颈）。"""
    kind = bottleneck_of(f)
    cands = [p for p in reg.all() if p.capacity_gb >= f.model_size_gb]
    if not cands:
        return None
    if kind == 'capacity':
        p = max(cands, key=lambda x: x.capacity_gb)
    elif kind == 'bandwidth':
        p = max(cands, key=lambda x: x.bandwidth_gb_s)
    elif kind == 'compute':
        p = max(cands, key=lambda x: x.compute_of(f.precision_pref or 'fp16'))
    else:
        p = max(cands, key=lambda x: x.capacity_gb + x.bandwidth_gb_s / 100.0)
    return p.node_id


def _baseline_pick(f: TaskFeature, reg: DeviceRegistry) -> Optional[str]:
    """基线：只看容量是否满足，取容量最大者（朴素贪心）。"""
    cands = [p for p in reg.all() if p.capacity_gb >= f.model_size_gb]
    if not cands:
        return None
    return max(cands, key=lambda x: x.capacity_gb).node_id


def _profile_pick(f: TaskFeature, reg: DeviceRegistry) -> Optional[str]:
    """画像路由：按瓶颈打分。"""
    cands = [p for p in reg.all() if p.capacity_gb >= f.model_size_gb]
    if not cands:
        return None
    if f.privacy:
        cands = [p for p in cands if p.has_tag('privacy-capable')] or cands
    return max(cands, key=lambda p: profile_bonus(f, p)).node_id


def run_ab(reg: DeviceRegistry,
           tasks: List[TaskFeature],
           tokens_per_task: int = 1000,
           hours_per_task: float = 0.01) -> ABResult:
    """执行一次 A/B 对比。

    返回命中率与估算成本；**真实时延需真实执行补齐**（见模块 docstring）。
    """
    res = ABResult(rounds=len(tasks))
    a_hit = b_hit = 0
    a_cost = b_cost = 0.0
    comparable = 0

    for f in tasks:
        ideal = _ideal_node(f, reg)
        a = _baseline_pick(f, reg)
        b = _profile_pick(f, reg)
        if ideal is None or a is None or b is None:
            res.detail.append({'skipped': True, 'ideal': ideal})
            continue
        comparable += 1
        a_hit += int(a == ideal)
        b_hit += int(b == ideal)
        pa, pb = reg.get(a), reg.get(b)
        a_cost += meter(pa, tokens=tokens_per_task, node_hours=hours_per_task).total_amount_cny
        b_cost += meter(pb, tokens=tokens_per_task, node_hours=hours_per_task).total_amount_cny
        res.detail.append({
            'ideal': ideal, 'A': a, 'B': b,
            'A_hit': a == ideal, 'B_hit': b == ideal,
        })

    if comparable:
        res.a_hit_rate = round(a_hit / comparable, 4)
        res.b_hit_rate = round(b_hit / comparable, 4)
        res.a_cost_cny = round(a_cost, 6)
        res.b_cost_cny = round(b_cost, 6)
        res.delta_hit_pp = round((res.b_hit_rate - res.a_hit_rate) * 100, 2)
        if res.a_cost_cny > 0:
            res.delta_cost_pct = round(
                (res.b_cost_cny - res.a_cost_cny) / res.a_cost_cny * 100, 2)
    return res


def _self_test() -> int:
    reg = DeviceRegistry()
    # 高带宽中等容量
    reg.register(DeviceProfile(
        node_id='5090-0', gpu_name='RTX-5090', family='consumer',
        capacity_gb=32, memory_model='discrete', bandwidth_gb_s=1792,
        compute_tflops={'fp8': 900}, precision_support=['fp8', 'fp16'],
        interconnect='none', power_w=575))
    # 高容量低带宽
    reg.register(DeviceProfile(
        node_id='gb10-0', gpu_name='GB10', family='soc',
        capacity_gb=128, memory_model='unified', bandwidth_gb_s=273,
        compute_tflops={'fp8': 300}, precision_support=['fp4', 'fp8'],
        interconnect='none', power_w=240))
    # 算力强
    reg.register(DeviceProfile(
        node_id='h100-0', gpu_name='H100', family='datacenter',
        capacity_gb=80, memory_model='discrete', bandwidth_gb_s=3350,
        compute_tflops={'fp8': 1979}, precision_support=['fp8', 'fp16'],
        interconnect='nvlink', power_w=700))

    tasks = [
        TaskFeature(model_size_gb=8, concurrency=32, phase='decode-heavy'),   # 带宽
        TaskFeature(model_size_gb=60, context_tokens=2000),                   # 容量
        TaskFeature(model_size_gb=8, phase='prefill-heavy', precision_pref='fp8'),  # 算力
        TaskFeature(model_size_gb=8, context_tokens=50000, phase='prefill-heavy'),  # 长上下文
        TaskFeature(model_size_gb=16),                                        # balanced
    ]

    r = run_ab(reg, tasks)
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    print('  基线命中率 %.2f%%，画像路由命中率 %.2f%%'
          % (r.a_hit_rate * 100, r.b_hit_rate * 100))
    check(r.rounds == 5, '任务数记录正确')
    check(r.b_hit_rate >= r.a_hit_rate, '画像路由命中率不低于基线')
    check(r.b_hit_rate > 0, '画像路由有命中')
    check(r.a_cost_cny > 0 and r.b_cost_cny > 0, '两侧成本均已估算')
    check(isinstance(r.delta_cost_pct, float), '成本变化百分比已计算')
    check(len(r.detail) == 5, '明细条数正确')

    # 极端：无设备满足 → 应跳过且不崩
    r2 = run_ab(reg, [TaskFeature(model_size_gb=9999)])
    check(r2.a_hit_rate == 0 and r2.b_hit_rate == 0, '容量不足时安全跳过（不崩溃）')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
