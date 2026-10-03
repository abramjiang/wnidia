# -*- coding: utf-8 -*-
"""画像路由（升级方案 ②）

设计原则：**叠加式，不推翻**。
现有 `scheduler.score()` 已综合容量余量/负载/时延/层级/声誉/密级/引擎健康，
工程完成度高。本模块只在现有打分之上**叠加一个"瓶颈匹配项"**，
硬约束（容量是否装得下、时延预算、密级、隐私）**完全复用、不重写**。

本质变化：
  现有路由回答 "哪个节点现在有空"
  升级后回答 "哪个节点最适合这类任务"

用法（植入方式）：
    from controller.profile_routing import profile_bonus, route
    # 在 scheduler.score() 末尾加一项：
    s += profile_bonus(feature, profile)      # 权重可配，默认保守
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

try:                                  # 包内导入
    from .device_profile import DeviceProfile, DeviceRegistry
except ImportError:                   # 独立运行（脚本自检）
    from device_profile import DeviceProfile, DeviceRegistry


# ---- 可调权重（默认保守，先观察再调大）----
W_CAPACITY = 1.0      # 容量瓶颈权重
W_BANDWIDTH = 1.0     # 带宽瓶颈权重
W_COMPUTE = 1.0       # 算力瓶颈权重
W_PRECISION_HIT = 6.0     # 精度命中加分
W_PRECISION_MISS = -8.0   # 精度不命中惩罚
SCALE_CAPACITY = 1.0      # 每 10GB 计 1 分
SCALE_BANDWIDTH = 1.0     # 每 1000GB/s 计 1 分
SCALE_COMPUTE = 1.0       # 每 1000 TFLOPS 计 1 分


@dataclass
class TaskFeature:
    """任务特征。现有调度看"节点状态"，这里补上"任务是什么"。"""
    model_size_gb: float = 0.0
    context_tokens: int = 0
    concurrency: int = 1
    phase: str = 'balanced'          # prefill-heavy | decode-heavy | balanced
    slo_latency_ms: float = 0.0      # 0 表示无硬约束
    privacy: bool = False            # True 则禁止离开本地（Mac 混编关键）
    precision_pref: str = ''         # 期望精度，如 'fp8'
    secret_rank: int = 0             # 沿用现有密级

    def validate(self) -> List[str]:
        errs = []
        if self.model_size_gb < 0 or self.concurrency < 1:
            errs.append('model_size_gb 不可为负，concurrency 至少为 1')
        if self.phase not in ('prefill-heavy', 'decode-heavy', 'balanced'):
            errs.append('phase 非法: %s' % self.phase)
        return errs

    def to_dict(self) -> Dict:
        return asdict(self)


def infer_feature(task: Dict) -> TaskFeature:
    """从任务字典推断特征（字段名兼容常见写法，缺项用保守默认）。"""
    d = task or {}
    size = float(d.get('model_size_gb') or d.get('need_mem_gb') or 0.0)
    ctx = int(d.get('context_tokens') or 0)
    conc = int(d.get('concurrency') or 1)
    phase = d.get('phase') or ''
    if not phase:
        # 无显式 phase 时按上下文长度粗判：长上下文多为 prefill-heavy
        phase = 'prefill-heavy' if ctx >= 8000 else 'balanced'
    return TaskFeature(
        model_size_gb=size,
        context_tokens=ctx,
        concurrency=conc,
        phase=phase,
        slo_latency_ms=float(d.get('slo_latency_ms') or d.get('latency_budget_ms') or 0.0),
        privacy=bool(d.get('privacy') or False),
        precision_pref=str(d.get('precision_pref') or ''),
        secret_rank=int(d.get('secret_rank') or 0),
    )


def bottleneck_of(f: TaskFeature) -> str:
    """判定该任务的主要瓶颈：容量 / 带宽 / 算力。"""
    if f.phase == 'decode-heavy' or f.concurrency >= 8:
        return 'bandwidth'
    if f.phase == 'prefill-heavy':
        return 'compute'
    if f.model_size_gb >= 40 or f.context_tokens >= 32000:
        return 'capacity'
    return 'balanced'


def profile_bonus(f: TaskFeature, p: DeviceProfile) -> float:
    """瓶颈匹配打分（叠加到现有 score 之上）。

    返回可正可负的加分项，调用方以 `s += profile_bonus(...)` 方式使用。
    """
    b = 0.0
    kind = bottleneck_of(f)

    if kind == 'capacity' or (f.model_size_gb > 0 and p.capacity_gb < f.model_size_gb):
        b += W_CAPACITY * (p.capacity_gb / 10.0) * SCALE_CAPACITY
    elif kind == 'bandwidth':
        b += W_BANDWIDTH * (p.bandwidth_gb_s / 1000.0) * SCALE_BANDWIDTH
    elif kind == 'compute':
        b += W_COMPUTE * (p.compute_of(f.precision_pref or 'fp16') / 1000.0) * SCALE_COMPUTE
    else:  # balanced
        b += 0.5 * (p.capacity_gb / 10.0) + 0.5 * (p.bandwidth_gb_s / 1000.0)

    # 精度匹配
    if f.precision_pref:
        b += W_PRECISION_HIT if p.supports(f.precision_pref) else W_PRECISION_MISS

    # 隐私任务必须留在本地节点
    if f.privacy and not p.has_tag('privacy-capable'):
        b += -100.0     # 等效于硬否决（但保留给调用方自行改为过滤）
    return b


def route(f: TaskFeature, reg: DeviceRegistry) -> Dict:
    """按画像路由，返回选中设备与解释（便于演示与审计）。

    硬约束（容量、隐私、精度支持）在此过滤；**时延预算与密级仍由现有 scheduler 负责**，
    本函数不重复实现，避免与现有逻辑冲突。
    """
    errs = f.validate()
    if errs:
        return {'ok': False, 'reason': '; '.join(errs), 'node': None}

    cands = list(reg.all())
    if not cands:
        return {'ok': False, 'reason': '无注册设备', 'node': None}

    # 硬约束 1：容量
    cands = [p for p in cands if p.capacity_gb >= f.model_size_gb]
    if not cands:
        return {'ok': False, 'reason': '无设备满足容量需求 %sGB' % f.model_size_gb, 'node': None}

    # 硬约束 2：隐私（不可离开本地）
    if f.privacy:
        cands = [p for p in cands if p.has_tag('privacy-capable')]
        if not cands:
            return {'ok': False, 'reason': '隐私任务但无本地(privacy-capable)设备', 'node': None}

    # 硬约束 3：精度（若显式要求且存在满足者，则只在满足者中选）
    if f.precision_pref:
        hit = [p for p in cands if p.supports(f.precision_pref)]
        if hit:
            cands = hit

    best = max(cands, key=lambda p: profile_bonus(f, p))
    return {
        'ok': True,
        'node': best.node_id,
        'profile': best.to_dict(),
        'bottleneck': bottleneck_of(f),
        'bonus': round(profile_bonus(f, best), 3),
        'reason': '瓶颈=%s，选中 %s（容量%sGB / 带宽%sGB/s）'
                  % (bottleneck_of(f), best.gpu_name, best.capacity_gb, best.bandwidth_gb_s),
        'candidates': [p.node_id for p in cands],
    }


def _self_test() -> int:
    reg = DeviceRegistry()
    reg.register(DeviceProfile(
        node_id='h100-0', gpu_name='H100', family='datacenter',
        capacity_gb=80, memory_model='discrete', bandwidth_gb_s=3350,
        compute_tflops={'fp8': 1979}, precision_support=['fp8', 'fp16'],
        interconnect='nvlink', power_w=700))
    reg.register(DeviceProfile(
        node_id='mac-01', gpu_name='Apple-M-Ultra', family='apple-silicon',
        capacity_gb=128, memory_model='unified', bandwidth_gb_s=800,
        compute_tflops={'fp16': 100}, precision_support=['fp16'],
        interconnect='none', power_w=200, tags=['local', 'privacy-capable']))
    reg.register(DeviceProfile(
        node_id='4090-0', gpu_name='RTX-4090', family='consumer',
        capacity_gb=24, memory_model='discrete', bandwidth_gb_s=1008,
        compute_tflops={'fp8': 660}, precision_support=['fp8', 'fp16'],
        interconnect='none', power_w=450))

    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    f_big = TaskFeature(model_size_gb=100, context_tokens=2000)
    r = route(f_big, reg)
    check(r['ok'] and r['node'] == 'mac-01', '大模型 100GB → 只有 Mac(128GB) 满足容量')

    f_conc = TaskFeature(model_size_gb=8, concurrency=32, phase='decode-heavy')
    r = route(f_conc, reg)
    check(r['ok'] and r['node'] == 'h100-0', '高并发 decode → 选最高带宽 H100')

    f_pref = TaskFeature(model_size_gb=8, phase='prefill-heavy', precision_pref='fp8')
    r = route(f_pref, reg)
    check(r['ok'] and r['node'] == 'h100-0', 'prefill-heavy + fp8 → 选 H100（算力与精度均命中）')

    f_priv = TaskFeature(model_size_gb=8, privacy=True)
    r = route(f_priv, reg)
    check(r['ok'] and r['node'] == 'mac-01', '隐私任务 → 强制留在 Mac')

    f_over = TaskFeature(model_size_gb=500)
    r = route(f_over, reg)
    check(not r['ok'] and '容量' in r['reason'], '容量 500GB → 正确拒绝并给出原因')

    f_bad = TaskFeature(model_size_gb=-1)
    check(not route(f_bad, reg)['ok'], '非法任务特征被拒绝')

    check(bottleneck_of(TaskFeature(concurrency=64)) == 'bandwidth', '瓶颈判定：高并发→带宽')
    check(bottleneck_of(TaskFeature(phase='prefill-heavy')) == 'compute', '瓶颈判定：prefill→算力')
    check(bottleneck_of(TaskFeature(model_size_gb=70)) == 'capacity', '瓶颈判定：大模型→容量')

    inf = infer_feature({'need_mem_gb': 12, 'concurrency': 4, 'latency_budget_ms': 300})
    check(inf.model_size_gb == 12 and inf.concurrency == 4 and inf.slo_latency_ms == 300,
          '从任务字典推断特征正确')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
