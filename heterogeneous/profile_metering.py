# -*- coding: utf-8 -*-
"""按画像计量（升级方案 ③）

在现有 `controller/metering.py`（meter_*/price_* 两层分离）之上扩展：

  现有：金额 = f(tokens) 或 f(node_hours) 或 f(seats)   —— 统一单价
  升级：金额 = 基础度量 × 设备成本系数 + 能耗成本        —— 按画像差异化

关键设计：**跨架构统一口径**
  Mac 与 NVIDIA 不比较"算力"，而比较"完成的同类任务量"（token / 任务数），
  设备差异只体现在成本系数上 → 两者可放在同一张账单上。
  这是 Mac + NVIDIA 混编能真正落地的前提。

⚠️ COST_PROFILE 中的系数为**占位设计**，必须由真实采购/折旧/电价数据替换。
"""

import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

try:
    from .device_profile import DeviceProfile
except ImportError:
    from device_profile import DeviceProfile


def _f(env, default):
    try:
        return float(os.getenv(env, default))
    except (TypeError, ValueError):
        return float(default)


# ---- 按 family 的成本系数（占位，需真实数据替换）----
DEFAULT_COST_PROFILE: Dict[str, float] = {
    'datacenter': 1.00,      # 基准（H/B 系列）
    'workstation': 0.60,
    'consumer': 0.45,        # 4090 / 5090
    'edge': 0.20,            # Jetson
    'soc': 0.30,             # GB10 类一体机
    'apple-silicon': 0.25,   # Mac（本地、边际成本低）
}

# ---- 计量单价（沿用现有 metering.py 口径，可通过环境变量覆盖）----
PRICE_TOKEN_PER_MILLION = _f('WNIDIA_PRICE_TOKEN', '2.0')
PRICE_NODE_HOUR = _f('WNIDIA_PRICE_NODE', '1.2')
ENERGY_PRICE_PER_KWH = _f('WNIDIA_PRICE_KWH', '0.6')   # 元/度，需按实际电价核


def cost_factor(profile: Optional[DeviceProfile]) -> float:
    """取设备成本系数；未知 family 回落 1.0（保守，不低估）。"""
    if profile is None:
        return 1.0
    return float(DEFAULT_COST_PROFILE.get(profile.family, 1.0))


def energy_cost(profile: Optional[DeviceProfile], hours: float) -> float:
    """能耗成本 = 功率(kW) × 小时 × 电价。"""
    if profile is None or hours <= 0:
        return 0.0
    kwh = (float(profile.power_w) / 1000.0) * float(hours)
    return round(kwh * ENERGY_PRICE_PER_KWH, 6)


@dataclass
class MeterRecord:
    """一次计量的明细（便于对账与审计）。"""
    node_id: str = ''
    family: str = ''
    base_mode: str = 'token'          # token | node
    tokens: int = 0
    node_hours: float = 0.0
    cost_factor: float = 1.0
    base_amount_cny: float = 0.0      # 基础度量金额
    energy_amount_cny: float = 0.0    # 能耗金额
    total_amount_cny: float = 0.0     # 合计
    estimated: bool = False           # 沿用现有语义：真实 usage vs 估算

    def to_dict(self) -> Dict:
        return asdict(self)


def meter(profile: Optional[DeviceProfile],
          tokens: int = 0,
          node_hours: float = 0.0,
          base_mode: str = 'token',
          estimated: bool = False,
          include_energy: bool = True) -> MeterRecord:
    """按画像计量。

    - base_mode='token'：以 token 为基础度量（跨架构可比，推荐）
    - base_mode='node'：以节点小时为基础度量
    - include_energy：是否计入能耗（默认计入）
    """
    if base_mode not in ('token', 'node'):
        base_mode = 'token'

    if base_mode == 'token':
        base = (float(tokens) / 1_000_000.0) * PRICE_TOKEN_PER_MILLION
    else:
        base = float(node_hours) * PRICE_NODE_HOUR

    f = cost_factor(profile)
    weighted = base * f
    energy = energy_cost(profile, node_hours) if include_energy else 0.0

    return MeterRecord(
        node_id=(profile.node_id if profile else ''),
        family=(profile.family if profile else ''),
        base_mode=base_mode,
        tokens=int(tokens),
        node_hours=round(float(node_hours), 6),
        cost_factor=f,
        base_amount_cny=round(weighted, 6),
        energy_amount_cny=energy,
        total_amount_cny=round(weighted + energy, 6),
        estimated=bool(estimated),
    )


def compare(records) -> Dict:
    """多设备计量对比——用于验证"跨架构统一口径"。

    返回按 total 排序的对照表，便于回答"在 Mac 上跑 vs 在 NVIDIA 上跑哪个划算"。
    """
    rs = [r.to_dict() if isinstance(r, MeterRecord) else r for r in records]
    rs = sorted(rs, key=lambda d: d.get('total_amount_cny', 0.0))
    cheapest = rs[0] if rs else None
    return {
        'records': rs,
        'cheapest': (cheapest.get('node_id') if cheapest else None),
        'cheapest_total_cny': (cheapest.get('total_amount_cny') if cheapest else 0.0),
    }


def _self_test() -> int:
    from device_profile import DeviceProfile   # 独立运行时

    h100 = DeviceProfile(node_id='h100-0', family='datacenter', power_w=700,
                         capacity_gb=80, bandwidth_gb_s=3350)
    mac = DeviceProfile(node_id='mac-01', family='apple-silicon', power_w=200,
                        capacity_gb=128, bandwidth_gb_s=800)
    unknown = DeviceProfile(node_id='x', family='unknown-family', power_w=100)

    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    r_h = meter(h100, tokens=1_000_000, node_hours=1.0)
    r_m = meter(mac, tokens=1_000_000, node_hours=1.0)

    check(r_h.cost_factor == 1.0, 'H100 成本系数 = 1.0（基准）')
    check(r_m.cost_factor == 0.25, 'Mac 成本系数 = 0.25')
    check(r_h.base_amount_cny == 2.0, 'H100 基础金额 = 2.0 元/百万 token')
    check(abs(r_m.base_amount_cny - 0.5) < 1e-9, 'Mac 基础金额 = 0.5 元/百万 token')
    check(r_h.energy_amount_cny > r_m.energy_amount_cny, 'H100 能耗成本高于 Mac（700W vs 200W）')
    check(r_h.total_amount_cny > r_m.total_amount_cny, '同任务下 Mac 总成本更低')

    c = compare([r_h, r_m])
    check(c['cheapest'] == 'mac-01', '对比结果：Mac 最划算')

    r_u = meter(unknown, tokens=1_000_000)
    check(r_u.cost_factor == 1.0, '未知 family 回落 1.0（保守，不低估）')

    r_n = meter(h100, node_hours=2.0, base_mode='node')
    check(r_n.base_mode == 'node' and r_n.total_amount_cny > 0, '节点小时口径可用')

    r_e = meter(h100, tokens=1000, node_hours=1.0, include_energy=False)
    check(r_e.energy_amount_cny == 0.0, '可关闭能耗项')

    r_est = meter(h100, tokens=1000, estimated=True)
    check(r_est.estimated is True, 'estimated 标记可透传（沿用现有语义）')

    r_bad = meter(h100, tokens=1000, base_mode='wrong')
    check(r_bad.base_mode == 'token', '非法口径回落 token')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
