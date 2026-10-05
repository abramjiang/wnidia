# -*- coding: utf-8 -*-
"""WNIDIA Routing SDK —— **零依赖**的画像路由内核（P3 · 提分路径）。

    Copyright 2026 abramjiang
    Apache License 2.0

## 它是什么

把「按硬件画像挑设备」这段逻辑抽成一个**只依赖标准库**的小模块，
不 import WNIDIA 的任何东西，因此可以**直接复制进第三方项目**
（或向上游贡献），而不必引入整套 WNIDIA。

## 为什么这么做

同类项目（如家庭/小型集群的推理代理）常自述"调度只有一种策略：
不看 GPU 型号、不看显存、不看利用率、不看时延"，并把
"更聪明的路由"列为最清晰的 roadmap 项。

这正是本 SDK 已经解决的问题。把它做成零依赖单文件，等于：
- 别人可以直接用（降低被集成门槛）
- 也可以被贡献出去（不需要先接受 WNIDIA 的架构）

## 用法（第三方项目视角）

    from routing_sdk import DeviceSpec, RequestSpec, recommend

    devices = [
        DeviceSpec('mac-0', capacity_gb=64, bandwidth_gb_s=800,
                   memory_model='unified', tags=['privacy-capable']),
        DeviceSpec('5090-0', capacity_gb=32, bandwidth_gb_s=1792,
                   compute_tflops={'fp8': 900}),
        DeviceSpec('h100-0', capacity_gb=80, bandwidth_gb_s=3350,
                   compute_tflops={'fp8': 1979}),
    ]
    r = recommend(devices, RequestSpec(model_size_gb=14, concurrency=32))
    print(r['node_id'], r['bottleneck'], r['reason'])

## 设计原则

1. **只做建议，不执行**：返回推荐结果与解释，不碰网络、不改状态。
2. **硬约束优先**：容量 / 隐私 / 精度不满足直接排除，不参与打分。
3. **可解释**：每次推荐都给出 `reason` 和完整 `ranking`，便于审计与调试。
4. **确定性**：同样输入必得同样输出（无随机、无时间依赖）。
"""

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

SDK_VERSION = '1.0.0'

# 打分权重（可按实测调优；当前为保守初值）
W_BANDWIDTH = 1.0
W_CAPACITY = 1.0
W_COMPUTE = 1.0
W_PRECISION_HIT = 2.0
W_PRECISION_MISS = -2.0
W_PRIVACY_VIOLATION = -100.0     # 等效硬否决（但保留在结果里便于排查）

DEFAULT_PRECISION = 'fp16'


# ---------------------------------------------------------------- 数据结构
@dataclass
class DeviceSpec:
    """设备画像。字段刻意保持最小集合——多了就难以被第三方采用。"""
    node_id: str
    gpu_name: str = 'unknown'
    vendor: str = 'nvidia'
    capacity_gb: float = 0.0
    bandwidth_gb_s: float = 0.0
    compute_tflops: Dict[str, float] = field(default_factory=dict)
    precision_support: List[str] = field(default_factory=list)
    memory_model: str = 'discrete'      # unified | discrete
    power_w: float = 0.0
    tags: List[str] = field(default_factory=list)

    def supports(self, precision: str) -> bool:
        return (not precision) or (precision in self.precision_support)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def compute_of(self, precision: str = DEFAULT_PRECISION) -> float:
        if not self.compute_tflops:
            return 0.0
        if precision in self.compute_tflops:
            return self.compute_tflops[precision]
        # 回退：优先取更低精度（通常更快），再取任意已知值
        for p in ('fp8', 'fp16', 'fp32'):
            if p in self.compute_tflops:
                return self.compute_tflops[p]
        return max(self.compute_tflops.values())

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> 'DeviceSpec':
        known = {f for f in DeviceSpec.__dataclass_fields__}
        return DeviceSpec(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def from_any(obj: Any) -> 'DeviceSpec':
        """从任意带同名属性的对象构造（鸭子类型）。

        这样第三方无需先接受 WNIDIA 的数据结构——只要字段同名即可。
        """
        g = lambda k, d=None: getattr(obj, k, d)
        return DeviceSpec(
            node_id=str(g('node_id', '') or ''),
            gpu_name=str(g('gpu_name', 'unknown') or 'unknown'),
            vendor=str(g('vendor', 'nvidia') or 'nvidia'),
            capacity_gb=float(g('capacity_gb', 0.0) or 0.0),
            bandwidth_gb_s=float(g('bandwidth_gb_s', 0.0) or 0.0),
            compute_tflops=dict(g('compute_tflops', {}) or {}),
            precision_support=list(g('precision_support', []) or []),
            memory_model=str(g('memory_model', 'discrete') or 'discrete'),
            power_w=float(g('power_w', 0.0) or 0.0),
            tags=list(g('tags', []) or []),
        )


@dataclass
class RequestSpec:
    """一次请求的特征。都由调用方从请求中推断，不假设它有额外信息。"""
    model_size_gb: float = 0.0
    context_tokens: int = 0
    concurrency: int = 1
    phase: str = 'balanced'        # prefill-heavy | decode-heavy | balanced
    privacy: bool = False          # True 则禁止离开带 privacy-capable 的设备
    precision_pref: str = ''

    def validate(self) -> List[str]:
        errs = []
        if self.model_size_gb < 0:
            errs.append('model_size_gb 不可为负')
        if self.concurrency < 1:
            errs.append('concurrency 至少为 1')
        if self.phase not in ('prefill-heavy', 'decode-heavy', 'balanced'):
            errs.append('phase 非法: %s' % self.phase)
        return errs

    def to_dict(self) -> Dict:
        return asdict(self)


# ---------------------------------------------------------------- 核心
def bottleneck_of(r: RequestSpec) -> str:
    """判断该请求的主要瓶颈。

    判断顺序有意为之：**容量是硬需求，先看装不装得下**；
    再看并发/输出长度（带宽），最后看算力。
    """
    if r.model_size_gb >= 60:
        return 'capacity'
    if r.concurrency >= 8 or r.phase == 'decode-heavy':
        return 'bandwidth'
    if r.phase == 'prefill-heavy' or r.context_tokens >= 4000:
        return 'compute'
    return 'balanced'


def score(r: RequestSpec, d: DeviceSpec) -> float:
    """画像匹配打分（越大越合适）。"""
    s = 0.0
    kind = bottleneck_of(r)

    if kind == 'capacity':
        s += W_CAPACITY * (d.capacity_gb / 10.0)
    elif kind == 'bandwidth':
        s += W_BANDWIDTH * (d.bandwidth_gb_s / 1000.0)
    elif kind == 'compute':
        s += W_COMPUTE * (d.compute_of(r.precision_pref or DEFAULT_PRECISION)
                          / 1000.0)
    else:
        s += 0.5 * (d.capacity_gb / 10.0) + 0.5 * (d.bandwidth_gb_s / 1000.0)

    if r.precision_pref:
        s += W_PRECISION_HIT if d.supports(r.precision_pref) else W_PRECISION_MISS
    if r.privacy and not d.has_tag('privacy-capable'):
        s += W_PRIVACY_VIOLATION
    return s


def recommend(devices: List[DeviceSpec], r: RequestSpec,
              allowed_nodes: Optional[List[str]] = None) -> Dict:
    """推荐一个设备。

    返回：
        ok / node_id / bottleneck / reason / ranking / excluded
    `excluded` 记录被排除的设备与原因——排查路由问题时这是最有用的字段。
    """
    errs = r.validate()
    if errs:
        return {'ok': False, 'reason': '; '.join(errs), 'node_id': '',
                'ranking': [], 'excluded': {}}

    pool = [DeviceSpec.from_any(d) if not isinstance(d, DeviceSpec) else d
            for d in devices]
    if allowed_nodes:
        pool = [d for d in pool if d.node_id in allowed_nodes]
    if not pool:
        return {'ok': False, 'reason': '无可用设备', 'node_id': '',
                'ranking': [], 'excluded': {}}

    excluded: Dict[str, str] = {}

    # 硬约束 1：容量
    fit = []
    for d in pool:
        if d.capacity_gb >= r.model_size_gb:
            fit.append(d)
        else:
            excluded[d.node_id] = '容量不足（%sGB < %sGB）' % (
                d.capacity_gb, r.model_size_gb)
    if not fit:
        return {'ok': False,
                'reason': '无设备满足容量需求 %sGB' % r.model_size_gb,
                'node_id': '', 'ranking': [], 'excluded': excluded}

    # 硬约束 2：隐私
    if r.privacy:
        keep = [d for d in fit if d.has_tag('privacy-capable')]
        for d in fit:
            if d not in keep:
                excluded[d.node_id] = '隐私请求但非 privacy-capable 设备'
        fit = keep
        if not fit:
            return {'ok': False, 'reason': '隐私请求但无本地(privacy-capable)设备',
                    'node_id': '', 'ranking': [], 'excluded': excluded}

    # 硬约束 3：精度（若有满足者，则只在满足者中选）
    if r.precision_pref:
        hit = [d for d in fit if d.supports(r.precision_pref)]
        if hit:
            for d in fit:
                if d not in hit:
                    excluded[d.node_id] = '不支持精度 %s' % r.precision_pref
            fit = hit

    ranked = sorted(((d.node_id, round(score(r, d), 4)) for d in fit),
                    key=lambda x: -x[1])
    best_id, best_score = ranked[0]
    best = next(d for d in fit if d.node_id == best_id)
    kind = bottleneck_of(r)

    return {
        'ok': True,
        'node_id': best_id,
        'bottleneck': kind,
        'score': best_score,
        'reason': '瓶颈=%s，选中 %s（容量%sGB / 带宽%sGB/s）'
                  % (kind, best.gpu_name, best.capacity_gb, best.bandwidth_gb_s),
        'ranking': ranked,
        'excluded': excluded,
        'sdk_version': SDK_VERSION,
    }


def recommend_from_dicts(devices: List[Dict], request: Dict) -> Dict:
    """纯 dict 入口（方便跨语言/配置化调用）。"""
    return recommend([DeviceSpec.from_dict(d) for d in devices],
                     RequestSpec(**{k: v for k, v in request.items()
                                    if k in RequestSpec.__dataclass_fields__}))


# ---------------------------------------------------------------- 自检
def _self_test() -> int:
    ok = 0
    fails = []

    def check(cond, msg):
        nonlocal ok
        if cond:
            ok += 1
            print('  ✅ %s' % msg)
        else:
            fails.append(msg)
            print('  ❌ %s' % msg)

    mac = DeviceSpec('mac-0', gpu_name='M4 Max', vendor='apple',
                     capacity_gb=64, bandwidth_gb_s=546,
                     compute_tflops={'fp16': 40}, precision_support=['fp16'],
                     memory_model='unified', power_w=80,
                     tags=['privacy-capable'])
    r5090 = DeviceSpec('5090-0', gpu_name='RTX 5090', capacity_gb=32,
                       bandwidth_gb_s=1792, compute_tflops={'fp8': 900,
                                                            'fp4': 1800},
                       precision_support=['fp4', 'fp8', 'fp16'], power_w=575)
    h100 = DeviceSpec('h100-0', gpu_name='H100', capacity_gb=80,
                      bandwidth_gb_s=3350, compute_tflops={'fp8': 1979},
                      precision_support=['fp8', 'fp16'], power_w=700)
    gb10 = DeviceSpec('gb10-0', gpu_name='GB10', capacity_gb=128,
                      bandwidth_gb_s=273, compute_tflops={'fp8': 300},
                      precision_support=['fp4', 'fp8'], memory_model='unified',
                      power_w=240, tags=['privacy-capable'])
    pool = [mac, r5090, h100, gb10]

    print('— 瓶颈识别 —')
    check(bottleneck_of(RequestSpec(model_size_gb=70)) == 'capacity',
          '大模型 → 容量瓶颈')
    check(bottleneck_of(RequestSpec(concurrency=32)) == 'bandwidth',
          '高并发 → 带宽瓶颈')
    check(bottleneck_of(RequestSpec(context_tokens=8000)) == 'compute',
          '长上下文 → 算力瓶颈')
    check(bottleneck_of(RequestSpec()) == 'balanced', '默认 → 均衡')

    print('— 路由正确性 —')
    r1 = recommend(pool, RequestSpec(model_size_gb=14, concurrency=32))
    check(r1['ok'] and r1['node_id'] == 'h100-0',
          '高并发中小模型 → 最高带宽 H100（实得 %s）' % r1['node_id'])

    r2 = recommend(pool, RequestSpec(model_size_gb=100))
    check(r2['ok'] and r2['node_id'] == 'gb10-0',
          '超大容量需求 → 仅 GB10(128GB) 满足（实得 %s）' % r2['node_id'])
    check('5090-0' in r2['excluded'] and '容量不足' in r2['excluded']['5090-0'],
          '被排除设备记录原因（便于排查）')

    r3 = recommend(pool, RequestSpec(model_size_gb=14, privacy=True))
    check(r3['ok'] and r3['node_id'] in ('mac-0', 'gb10-0'),
          '隐私请求 → 只在 privacy-capable 中选（实得 %s）' % r3['node_id'])

    r4 = recommend(pool, RequestSpec(model_size_gb=14, precision_pref='fp4'))
    check(r4['ok'] and r4['node_id'] in ('5090-0', 'gb10-0'),
          '要求 fp4 → 只在支持 fp4 的设备中选（实得 %s）' % r4['node_id'])

    r5 = recommend(pool, RequestSpec(model_size_gb=999))
    check(not r5['ok'] and '容量' in r5['reason'],
          '容量无解 → 明确失败而非硬塞')

    print('— 排序与确定性 —')
    check(r1['ranking'] == sorted(r1['ranking'], key=lambda x: -x[1]),
          'ranking 按得分降序')
    check(recommend(pool, RequestSpec(model_size_gb=14,
                                      concurrency=32))['node_id']
          == r1['node_id'], '同样输入必得同样输出（确定性）')

    print('— 边界与兼容 —')
    check(recommend([], RequestSpec())['ok'] is False, '空设备池 → 明确失败')
    bad = recommend(pool, RequestSpec(concurrency=0))
    check(not bad['ok'] and 'concurrency' in bad['reason'],
          '非法参数被拒绝（concurrency=0）')

    d = mac.to_dict()
    back = DeviceSpec.from_dict(d)
    check(back.node_id == mac.node_id and back.capacity_gb == mac.capacity_gb,
          'dict 往返一致（便于配置化）')

    class Foreign:      # 模拟第三方项目的设备对象（鸭子类型）
        node_id = 'foreign-0'
        gpu_name = 'Some GPU'
        vendor = 'other'
        capacity_gb = 48.0
        bandwidth_gb_s = 1000.0
        compute_tflops = {'fp16': 100.0}
        precision_support = ['fp16']
        memory_model = 'discrete'
        power_w = 300.0
        tags = []
    rf = recommend([Foreign()], RequestSpec(model_size_gb=10))
    check(rf['ok'] and rf['node_id'] == 'foreign-0',
          '鸭子类型适配：第三方对象无需改造即可接入')

    print('— 纯 dict 入口 —')
    rd = recommend_from_dicts([h100.to_dict(), gb10.to_dict()],
                              {'model_size_gb': 100})
    check(rd['ok'] and rd['node_id'] == 'gb10-0',
          'dict 入口可用（跨语言/配置化）')

    total = ok + len(fails)
    print('\n自检: %s (%d/%d)' % ('ALL PASS' if not fails else 'HAS FAIL',
                                  ok, total))
    return 0 if not fails else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
