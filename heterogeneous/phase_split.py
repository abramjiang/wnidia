# -*- coding: utf-8 -*-
"""M4 · Prefill / Decode 分离规划器

原理：
  - Prefill（算力密集）→ 高算力设备
  - Decode（带宽密集）→ 高带宽设备
  - KV cache（容量密集）→ 高容量设备

⚠️ 诚实边界：
  - 分离**不一定更快**：跨设备要传 KV，传输开销可能吃掉收益；
    本模块会做开销/收益估算，不划算时返回 `split=False`（合并执行）。
  - 仅做**实例级分离**，不做算子级异构并行（避免木桶效应）。
  - 现有 `NodeProfile.role`（prefill/decode/cpu）已有静态角色匹配，
    本模块做的是"按任务阶段动态判定"，不改变现有匹配机制。

传输开销估算为**简化模型**，需实测校准：
    kv_bytes ≈ context_tokens × hidden_per_token × dtype_bytes
    transfer_ms ≈ kv_bytes / ( interconnect_gb_s × 1e9 ) × 1000
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

try:
    from .device_profile import DeviceProfile, DeviceRegistry
    from .profile_routing import TaskFeature, bottleneck_of
except ImportError:
    from device_profile import DeviceProfile, DeviceRegistry
    from profile_routing import TaskFeature, bottleneck_of

# 简化参数（需实测校准）
HIDDEN_BYTES_PER_TOKEN = 2 * 1024 * 2      # 近似：2 向量 × 1024 维 × fp16(2B)
INTERCONNECT_GB_S = {                       # 按互联类型的有效带宽（GB/s）
    'nvlink': 300.0,
    'pcie': 16.0,
    'none': 8.0,        # 走网络，最慢
}
MIN_GAIN_MS = 50.0      # 分离至少要节省这么多毫秒才值得


def _conn_gb_s(p: Optional[DeviceProfile]) -> float:
    if p is None:
        return INTERCONNECT_GB_S['none']
    return float(INTERCONNECT_GB_S.get(p.interconnect, INTERCONNECT_GB_S['none']))


def estimate_transfer_ms(f: TaskFeature, src: Optional[DeviceProfile],
                         dst: Optional[DeviceProfile]) -> float:
    """估算 KV 从 src 传到 dst 的耗时（毫秒）。同设备则为 0。"""
    if src is None or dst is None or src.node_id == dst.node_id:
        return 0.0
    kv_bytes = float(f.context_tokens or 0) * HIDDEN_BYTES_PER_TOKEN
    if kv_bytes <= 0:
        return 0.0
    bw = min(_conn_gb_s(src), _conn_gb_s(dst)) * 1e9   # bytes/s
    if bw <= 0:
        return float('inf')
    return (kv_bytes / bw) * 1000.0


@dataclass
class SplitPlan:
    split: bool = False
    prefill_node: Optional[str] = None
    decode_node: Optional[str] = None
    kv_node: Optional[str] = None
    transfer_ms: float = 0.0
    reason: str = ''

    def to_dict(self) -> Dict:
        return asdict(self)


def plan(f: TaskFeature, reg: DeviceRegistry) -> SplitPlan:
    """生成分阶段执行计划。"""
    cands = [p for p in reg.all() if p.capacity_gb >= f.model_size_gb]
    if not cands:
        return SplitPlan(split=False, reason='无设备满足容量需求 %sGB' % f.model_size_gb)

    if f.privacy:
        cands = [p for p in cands if p.has_tag('privacy-capable')]
        if not cands:
            return SplitPlan(split=False, reason='隐私任务但无本地设备')

    prefill = max(cands, key=lambda p: p.compute_of(f.precision_pref or 'fp16'))
    decode = max(cands, key=lambda p: p.bandwidth_gb_s)
    kv = max(cands, key=lambda p: p.capacity_gb)

    # 若两个阶段落在同一设备 → 不分离
    if prefill.node_id == decode.node_id:
        return SplitPlan(split=False,
                         prefill_node=prefill.node_id,
                         decode_node=decode.node_id,
                         kv_node=kv.node_id,
                         transfer_ms=0.0,
                         reason='prefill 与 decode 落在同一设备，无需分离')

    cost = estimate_transfer_ms(f, prefill, decode)
    # 收益估计（简化）：decode 阶段带宽差带来的收益，随并发放大
    bw_gain = max(0.0, decode.bandwidth_gb_s - prefill.bandwidth_gb_s)
    benefit_ms = (bw_gain / max(prefill.bandwidth_gb_s, 1.0)) * 10.0 * max(f.concurrency, 1)

    if benefit_ms - cost >= MIN_GAIN_MS:
        return SplitPlan(split=True, prefill_node=prefill.node_id,
                         decode_node=decode.node_id, kv_node=kv.node_id,
                         transfer_ms=round(cost, 3),
                         reason='分离收益 %.1fms > 开销 %.1fms + 阈值'
                                % (benefit_ms, cost))
    return SplitPlan(split=False, prefill_node=prefill.node_id,
                     decode_node=decode.node_id, kv_node=kv.node_id,
                     transfer_ms=round(cost, 3),
                     reason='传输开销 %.1fms 抵消收益 %.1fms，不分离（合并执行）'
                            % (cost, benefit_ms))


def _self_test() -> int:
    reg = DeviceRegistry()
    reg.register(DeviceProfile(
        node_id='h100-0', gpu_name='H100', family='datacenter',
        capacity_gb=80, bandwidth_gb_s=3350, compute_tflops={'fp8': 1979},
        interconnect='nvlink', power_w=700))
    reg.register(DeviceProfile(
        node_id='gb10-0', gpu_name='GB10', family='soc',
        capacity_gb=128, bandwidth_gb_s=273, compute_tflops={'fp8': 300},
        interconnect='none', power_w=240))

    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    # 高算力与高带宽分属两设备 → 可能分离
    p1 = plan(TaskFeature(model_size_gb=8, concurrency=64,
                          context_tokens=2000, phase='decode-heavy'), reg)
    check(p1.prefill_node == 'h100-0', 'prefill → 算力最高的 H100')
    check(p1.decode_node == 'h100-0', 'decode → 带宽最高的 H100（本例同设备）')
    check(p1.split is False and '同一设备' in p1.reason, '同设备时明确不分离')

    # 强制两设备不同的场景：仅注册低功耗设备 + 高带宽设备
    reg2 = DeviceRegistry()
    reg2.register(DeviceProfile(
        node_id='compute-0', family='datacenter', capacity_gb=80,
        bandwidth_gb_s=100, compute_tflops={'fp8': 1979}, interconnect='nvlink'))
    reg2.register(DeviceProfile(
        node_id='bandwidth-0', family='consumer', capacity_gb=32,
        bandwidth_gb_s=1792, compute_tflops={'fp8': 300}, interconnect='pcie'))
    p2 = plan(TaskFeature(model_size_gb=8, concurrency=64,
                          context_tokens=1000, phase='decode-heavy'), reg2)
    check(p2.prefill_node == 'compute-0' and p2.decode_node == 'bandwidth-0',
          '分离场景：prefill 与 decode 分属不同设备')
    check(isinstance(p2.split, bool) and p2.reason != '', '给出分离与否的明确理由')

    # 长上下文导致传输开销巨大 → 应倾向于不分离
    p3 = plan(TaskFeature(model_size_gb=8, concurrency=2,
                          context_tokens=500000, phase='decode-heavy'), reg2)
    check(p3.transfer_ms > 0, '长上下文传输开销被计算（%.1fms）' % p3.transfer_ms)
    check(p3.reason != '', '长上下文给出决策理由')

    # 容量不足
    p4 = plan(TaskFeature(model_size_gb=9999), reg)
    check(p4.split is False and '容量' in p4.reason, '容量不足时安全拒绝（不崩溃）')

    # 隐私
    p5 = plan(TaskFeature(model_size_gb=8, privacy=True), reg)
    check(p5.split is False and '隐私' in p5.reason, '无本地设备时隐私任务被拒绝')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
