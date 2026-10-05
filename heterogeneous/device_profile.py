# -*- coding: utf-8 -*-
"""DeviceProfile —— 设备画像抽象（升级方案 ①）

设计原则：
  1. **不替换** 现有 `models.NodeProfile`，只作为补充结构 —— 渐进接入，不破坏现有调度
  2. 新增一种硬件**只注册画像**，不改调度逻辑 —— 这是"纳管"而非"适配"的关键
  3. 一张表同时容纳 CUDA 与 Metal(Apple Silicon) 设备，差异只体现在字段值上

与现有字段的关系（重要）：
  - `NodeProfile.bandwidth_mbps` 是**上行网络带宽(Mbps)**，不是显存带宽
  - 本结构的 `bandwidth_gb_s` 才是**显存带宽(GB/s)**，是异构路由的核心维度，不可混淆

字段用途：
  capacity_gb / memory_model → 容量瓶颈路由（大模型、长上下文）
  bandwidth_gb_s            → 带宽瓶颈路由（高并发 decode）
  compute_tflops            → 算力瓶颈路由（prefill-heavy）
  precision_support         → 量化策略与落点匹配
  family / arch             → 成本计量系数与能力推导
  tags                      → 隐私路由（如 privacy-capable / local）
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# ---- 受控词表 ----
FAMILIES = ('datacenter', 'workstation', 'consumer', 'edge', 'soc', 'apple-silicon')
MEMORY_MODELS = ('unified', 'discrete')
# 互联类型：随产业演进扩展（UALink 开放互联联盟、CXL 内存语义、UCIe 芯粒互联）
INTERCONNECTS = ('nvlink', 'ualink', 'cxl', 'ucie', 'pcie', 'none')
PRECISIONS = ('fp4', 'fp8', 'int8', 'fp16', 'fp32')
# 厂商：多厂商异构必需（范围限定美股大厂，不涉足国产卡）
VENDORS = ('nvidia', 'amd', 'intel', 'other')


@dataclass
class DeviceProfile:
    """设备画像。所有字段均可在注册时提供；缺省值保守，避免误判。"""
    node_id: str
    gpu_name: str = 'unknown'
    vendor: str = 'nvidia'                   # 见 VENDORS（nvidia / amd / intel / other）
    family: str = 'consumer'                 # 见 FAMILIES
    arch: str = ''                           # blackwell / ada / hopper / ampere / cdna / gaudi
    capacity_gb: float = 0.0                 # 可用容量预算
    memory_model: str = 'discrete'           # unified | discrete
    bandwidth_gb_s: float = 0.0              # 显存带宽（GB/s）
    compute_tflops: Dict[str, float] = field(default_factory=dict)
    precision_support: List[str] = field(default_factory=list)
    interconnect: str = 'none'               # 见 INTERCONNECTS
    power_w: int = 0
    engine_pref: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    # ---------------- 校验 ----------------
    def validate(self) -> List[str]:
        """返回问题列表；空列表表示合法。"""
        errs = []
        if not self.node_id:
            errs.append('node_id 不能为空')
        if self.family not in FAMILIES:
            errs.append('family 非法: %s（应为 %s）' % (self.family, ', '.join(FAMILIES)))
        if self.memory_model not in MEMORY_MODELS:
            errs.append('memory_model 非法: %s' % self.memory_model)
        if self.interconnect not in INTERCONNECTS:
            errs.append('interconnect 非法: %s' % self.interconnect)
        if self.vendor not in VENDORS:
            errs.append('vendor 非法: %s（应为 %s）' % (self.vendor, ', '.join(VENDORS)))
        if self.capacity_gb < 0 or self.bandwidth_gb_s < 0 or self.power_w < 0:
            errs.append('数值字段不可为负')
        bad = [p for p in self.precision_support if p not in PRECISIONS]
        if bad:
            errs.append('precision_support 含未知项: %s' % bad)
        return errs

    def is_valid(self) -> bool:
        return not self.validate()

    # ---------------- 能力查询 ----------------
    def supports(self, precision: str) -> bool:
        return precision in self.precision_support

    def compute_of(self, precision: str) -> float:
        """取指定精度算力；无该精度则退回任意最大值的一半（保守）。"""
        v = self.compute_tflops.get(precision)
        if v is not None:
            return float(v)
        vals = [float(x) for x in self.compute_tflops.values()]
        return (max(vals) / 2.0) if vals else 0.0

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> 'DeviceProfile':
        known = {f for f in DeviceProfile.__dataclass_fields__}
        return DeviceProfile(**{k: v for k, v in (d or {}).items() if k in known})


class DeviceRegistry:
    """设备注册表：新增硬件只注册画像，不改调度逻辑。"""

    def __init__(self):
        self._items: Dict[str, DeviceProfile] = {}

    def register(self, p: DeviceProfile) -> List[str]:
        errs = p.validate()
        if errs:
            return errs
        self._items[p.node_id] = p
        return []

    def get(self, node_id: str) -> Optional[DeviceProfile]:
        return self._items.get(node_id)

    def all(self) -> List[DeviceProfile]:
        return list(self._items.values())

    def by_family(self, family: str) -> List[DeviceProfile]:
        return [p for p in self._items.values() if p.family == family]

    def by_tag(self, tag: str) -> List[DeviceProfile]:
        return [p for p in self._items.values() if p.has_tag(tag)]

    # ---- 按瓶颈选设备（供升级方案 ② 的路由消费）----
    def best_for_capacity(self, need_gb: float) -> Optional[DeviceProfile]:
        """容量优先：返回能满足需求且容量最大的设备。"""
        cands = [p for p in self._items.values() if p.capacity_gb >= need_gb]
        return max(cands, key=lambda p: p.capacity_gb) if cands else None

    def best_for_bandwidth(self) -> Optional[DeviceProfile]:
        """带宽优先：高并发 decode。"""
        cands = list(self._items.values())
        return max(cands, key=lambda p: p.bandwidth_gb_s) if cands else None

    def best_for_compute(self, precision: Optional[str] = None) -> Optional[DeviceProfile]:
        """算力优先：prefill-heavy。可指定精度。"""
        cands = list(self._items.values())
        if not cands:
            return None
        if precision:
            cands = [p for p in cands if p.supports(precision)] or cands
        return max(cands, key=lambda p: p.compute_of(precision or 'fp16'))

    def to_list(self) -> List[Dict]:
        return [p.to_dict() for p in self._items.values()]


# ---- 模块级默认注册表 ----
# M2 接入后，scheduler / metering 需要按 node_id 查询画像，
# 这里提供单例注册表与便捷函数，避免各处重复创建。
DEFAULT_REGISTRY = DeviceRegistry()


def register_profile(p: DeviceProfile) -> List[str]:
    """注册到默认注册表（供 scheduler / metering 直接消费）。"""
    return DEFAULT_REGISTRY.register(p)


def profile_of(node_id: str) -> Optional[DeviceProfile]:
    """按 node_id 取画像；未注册返回 None（调用方应按降级处理）。

    ⚠️ 易错点：本函数只查**模块级 DEFAULT_REGISTRY**。
    若你用自建的 `DeviceRegistry()` 注册（如测试或试点脚本），
    请改用该实例的 `.get(node_id)`，否则会取到 None。
    """
    return DEFAULT_REGISTRY.get(node_id)


def _self_test() -> int:
    """内置自检：验证校验、能力查询与按瓶颈选设备。"""
    reg = DeviceRegistry()

    h100 = DeviceProfile(
        node_id='h100-0', gpu_name='H100', family='datacenter', arch='hopper',
        capacity_gb=80, memory_model='discrete', bandwidth_gb_s=3350,
        compute_tflops={'fp8': 1979, 'fp16': 989},
        precision_support=['fp8', 'int8', 'fp16'], interconnect='nvlink',
        power_w=700, engine_pref=['vllm', 'trtllm'],
    )
    mac = DeviceProfile(
        node_id='mac-01', gpu_name='Apple-M-Ultra', family='apple-silicon', arch='apple',
        capacity_gb=128, memory_model='unified', bandwidth_gb_s=800,
        compute_tflops={'fp16': 100},
        precision_support=['fp16', 'int8'], interconnect='none',
        power_w=200, engine_pref=['mlx', 'llama.cpp'],
        tags=['local', 'privacy-capable'],
    )
    bad = DeviceProfile(node_id='x', family='unknown-family')

    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    check(h100.is_valid(), 'H100 画像合法')
    check(mac.is_valid(), 'Mac 画像合法')
    check(not bad.is_valid(), '非法 family 被拒绝')
    check(reg.register(h100) == [] and reg.register(mac) == [], '注册成功')
    check(reg.register(bad) != [], '非法画像注册被拒')

    check(reg.best_for_capacity(100) is not None
          and reg.best_for_capacity(100).node_id == 'mac-01',
          '容量 100GB → 选 Mac（128GB 统一内存）')
    check(reg.best_for_capacity(200) is None, '容量 200GB → 无设备满足')
    check(reg.best_for_bandwidth().node_id == 'h100-0', '带宽优先 → 选 H100')
    check(reg.best_for_compute('fp8').node_id == 'h100-0', '算力优先(fp8) → 选 H100')
    check(reg.best_for_compute('fp4') is not None, '无 fp4 设备时仍能回退回选')
    check(len(reg.by_tag('privacy-capable')) == 1, '隐私标签设备可筛选')
    check(h100.supports('fp8') and not h100.supports('fp4'), '精度支持查询正确')
    check(mac.memory_model == 'unified' and h100.memory_model == 'discrete',
          '内存模型语义正确（统一 vs 独立显存）')
    check(DeviceProfile.from_dict(mac.to_dict()).node_id == 'mac-01', '序列化往返正确')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
