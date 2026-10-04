# -*- coding: utf-8 -*-
"""5090 + GB10 异构试点（首个落地案例）

为什么先做这一对：
  - GB10（128GB 统一内存，带宽 ≈273 GB/s）= **容量型**
  - RTX 5090（32GB，带宽 ≈1792 GB/s）= **带宽算力型**
  - 二者反差最大（容量 4×、带宽 6.5×），最能验证"按瓶颈路由"的价值
  - 用具体 pair 跑出数字，再推广到 A100/H100/4090/Jetson 全系列

本模块为**可运行的完整试点**，不依赖真实 GPU（纯逻辑 + 估算计量），
用于验证路由决策与成本模型；真实时延仍需真机执行。

运行：
    python3 controller/pilot_5090_gb10.py
"""

from typing import Dict, List

try:
    from .device_profile import DeviceProfile, DeviceRegistry, profile_of
    from .profile_routing import TaskFeature, route, infer_feature
    from .profile_metering import meter, compare
    from .profile_ab import run_ab
    from .phase_split import plan
except ImportError:
    from device_profile import DeviceProfile, DeviceRegistry, profile_of
    from profile_routing import TaskFeature, route, infer_feature
    from profile_metering import meter, compare
    from profile_ab import run_ab
    from phase_split import plan


# ---- 设备画像（参数需按官方 datasheet 复核）----
GB10 = dict(
    node_id='gb10-0', gpu_name='GB10', family='soc', arch='blackwell',
    capacity_gb=128, memory_model='unified', bandwidth_gb_s=273,
    compute_tflops={'fp8': 300, 'fp4': 600}, precision_support=['fp4', 'fp8'],
    interconnect='none', power_w=240, engine_pref=['ollama'],
)

RTX5090 = dict(
    node_id='5090-0', gpu_name='RTX-5090', family='consumer', arch='blackwell',
    capacity_gb=32, memory_model='discrete', bandwidth_gb_s=1792,
    compute_tflops={'fp8': 900, 'fp4': 2000}, precision_support=['fp4', 'fp8'],
    interconnect='none', power_w=575, engine_pref=['vllm', 'ollama'],
)


def build_registry() -> DeviceRegistry:
    """构造 5090 + GB10 双节点画像。"""
    reg = DeviceRegistry()
    for p in (DeviceProfile(**GB10), DeviceProfile(**RTX5090)):
        errs = reg.register(p)
        if errs:
            raise ValueError('画像非法: %s' % errs)
    return reg


# ---- 试点场景 ----
SCENARIOS: List[Dict] = [
    {'name': '高并发 decode（小模型）', 'expect': '5090-0',
     'task': dict(model_size_gb=8, concurrency=32, phase='decode-heavy')},
    {'name': '大模型 100GB', 'expect': 'gb10-0',
     'task': dict(model_size_gb=100, context_tokens=2000)},
    {'name': 'prefill-heavy', 'expect': '5090-0',
     'task': dict(model_size_gb=8, phase='prefill-heavy', precision_pref='fp8')},
    {'name': '长上下文 128k', 'expect': 'gb10-0',
     'task': dict(model_size_gb=60, context_tokens=128000)},
]


def run_pilot(tokens: int = 1_000_000, hours: float = 1.0) -> Dict:
    """执行完整试点，返回报告。"""
    reg = build_registry()
    report = {'scenarios': [], 'cost': None, 'ab': None, 'split': None}

    # 1) 路由场景
    for s in SCENARIOS:
        f = TaskFeature(**s['task'])
        r = route(f, reg)
        report['scenarios'].append({
            'name': s['name'],
            'expect': s['expect'],
            'actual': r.get('node'),
            'ok': r.get('ok') and r.get('node') == s['expect'],
            'bottleneck': r.get('bottleneck'),
            'reason': r.get('reason'),
        })

    # 2) 成本对比（同任务在两个节点）
    # 注意：用 reg.get() 而非 profile_of() —— 后者查的是模块级 DEFAULT_REGISTRY，
    # 本试点注册在自己的 registry 中，用 profile_of 会取到 None。
    recs = [meter(reg.get('gb10-0'), tokens=tokens, node_hours=hours),
            meter(reg.get('5090-0'), tokens=tokens, node_hours=hours)]
    report['cost'] = compare(recs)

    # 3) A/B 验证
    tasks = [TaskFeature(**s['task']) for s in SCENARIOS]
    ab = run_ab(reg, tasks)
    report['ab'] = {'a_hit_rate': ab.a_hit_rate, 'b_hit_rate': ab.b_hit_rate,
                    'delta_hit_pp': ab.delta_hit_pp,
                    'delta_cost_pct': ab.delta_cost_pct}

    # 4) 分离规划
    sp = plan(TaskFeature(model_size_gb=8, concurrency=32,
                          context_tokens=2000, phase='decode-heavy'), reg)
    report['split'] = {'split': sp.split, 'prefill': sp.prefill_node,
                       'decode': sp.decode_node, 'reason': sp.reason}
    return report


def _self_test() -> int:
    rep = run_pilot()
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    print('  —— 路由场景 ——')
    for s in rep['scenarios']:
        check(s['ok'], '%s → %s（期望 %s）' % (s['name'], s['actual'], s['expect']))

    print('  —— 成本 ——')
    check(rep['cost']['cheapest'] in ('gb10-0', '5090-0'),
          '成本对比可用：最划算 %s' % rep['cost']['cheapest'])

    print('  —— A/B ——')
    ab = rep['ab']
    print('     命中率 基线 %.0f%% → 画像路由 %.0f%%（+%s pp）'
          % (ab['a_hit_rate'] * 100, ab['b_hit_rate'] * 100, ab['delta_hit_pp']))
    check(ab['b_hit_rate'] >= ab['a_hit_rate'], '画像路由命中率不低于基线')

    print('  —— 分离 ——')
    check(rep['split']['reason'] != '',
          '分离规划给出理由：split=%s' % rep['split']['split'])

    # 边界：容量超出两者
    r = route(TaskFeature(model_size_gb=500), build_registry())
    check(not r['ok'], '容量 500GB → 正确拒绝（无设备满足）')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import json
    import sys
    rc = _self_test()
    print('\n—— 完整报告 ——')
    print(json.dumps(run_pilot(), ensure_ascii=False, indent=2))
    sys.exit(rc)
