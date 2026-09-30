# -*- coding: utf-8 -*-
"""WNIDIA 收益测量（可复现）。

四部分：
  A 校验 token 经济：用“真实 mock 预检路由”统计三种策略的节点调用 / token / 成本；
  B GPU 切分整合：用“真实 gpu-slicer.plan”对比 一模型一卡 vs 单卡整合；
  C 抢占回收：按显式假设估算每日回收的 GPU 时间（标 *）；
  D 闲机纳管：按显式场景估算新增可用容量（标 *）。

口径声明：
  - A/B 由仓库真实代码计算（A 的答案在 mock 后端、B 的 workload 为建模输入）；
  - C/D 为场景假设，标 *，真机需用 DCGM/GenAI-Perf 标定；
  - 托管模型价格仅用于“折算参照”，WNIDIA 真机成本以 GPU 时间为准。

用法： python bench/benefit.py            # 打印 + 写 bench/benefit_result.json
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from controller import config, jevclient           # noqa: E402
from worker import mock_compute                     # noqa: E402


def _load_tool(skill_dir, mod_name):
    path = os.path.join(ROOT, 'skills', skill_dir, 'tool.py')
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


slicer = _load_tool('gpu-slicer', 'gpu_slicer_tool')

# ---------------- 口径常量（显式、可改） ----------------
N = 1000                       # 每 1000 个任务
# 任务结构：60% chat(128) / 30% heavy(1500) / 10% batch(3000)
AVG_IN = round(0.6 * 128 + 0.3 * 1500 + 0.1 * 3000)   # 平均输入 tokens
OUT = 33                        # 实测 mock 诚实答案输出 tokens（见探查）
Q_SWEEP = (0.05, 0.10, 0.20, 0.30)   # 存疑/坏答案占比扫描
# 托管模型折算价格（$/百万 token，仅参照）
P_IN, P_OUT, P_JEV_IN = 0.50, 2.00, 0.042


def _usd(tokens, price_per_million):
    return tokens / 1_000_000 * price_per_million


# ===================================================================
# A. 校验 token 经济
# ===================================================================
def part_a():
    rows = {}

    def strat(name, node_calls, jev_in_tokens, detect):
        in_tok = node_calls * AVG_IN
        out_tok = node_calls * OUT
        gpu_cost = _usd(in_tok, P_IN) + _usd(out_tok, P_OUT)
        jev_cost = _usd(jev_in_tokens, P_JEV_IN)
        rows[name] = {
            'node_calls': node_calls,
            'gpu_input_tokens': in_tok, 'gpu_output_tokens': out_tok,
            'jev_input_tokens': jev_in_tokens,
            'modeled_cost_usd': round(gpu_cost + jev_cost, 4),
            'detection_coverage': detect,
        }
        return rows[name]

    # S0 不校验
    s0 = strat('S0_no_verify', N, 0, 0.0)
    # S1 每个任务三节点多数决
    s1 = strat('S1_full_majority', 3 * N, 0, 1.0)

    adaptive = {}
    for q in Q_SWEEP:
        jevclient.reset_counters()
        n_bad = int(round(N * q))
        node_calls = 0
        caught_bad = 0
        for i in range(N):
            bad = i < n_bad
            prompt = f'任务{i} prompt'
            ans = (mock_compute.one_shot('cheat prompt', 128, cheat=True)
                   if bad else mock_compute.one_shot(prompt, 128))
            pre = jevclient.precheck_answer(prompt, ans)
            confident = pre['p_correct'] >= config.JEV_VERIFY_TRIGGER
            node_calls += 1 if confident else 3
            if bad and not confident:
                caught_bad += 1
        # 每个任务一次预检：Jev 输入约为 prompt+answer
        jev_in = N * (AVG_IN + OUT)
        r = strat(f'S2_adaptive_q{int(q*100)}', node_calls, jev_in,
                  round(caught_bad / max(n_bad, 1), 3))
        r['skip_rate'] = round(jevclient.C['skips'] / N, 3)
        r['bad_caught_measured'] = caught_bad
        r['bad_total'] = n_bad
        adaptive[f'q{int(q*100)}'] = r

    # 以 q=10% 为代表，计算相对 S1 的节省
    rep = adaptive[f'q{int(0.10*100)}']
    savings = {
        'vs_full_majority_node_calls_saved_pct': round(
            (s1['node_calls'] - rep['node_calls']) / s1['node_calls'], 3),
        'vs_full_majority_cost_saved_pct': round(
            (s1['modeled_cost_usd'] - rep['modeled_cost_usd'])
            / s1['modeled_cost_usd'], 3),
        # 相对 S0（完全不校验）的开销倍数：用 s0 的真实节点调用数算，
        # 不再硬编码 2.0（硬编码在 N 变化或修改 S0 定义后会悄悄失真）。
        'verification_overhead_vs_no_verify_full_pct': round(
            s1['node_calls'] / max(s0['node_calls'], 1) - 1, 3),
        'verification_overhead_vs_no_verify_adaptive_pct': round(
            rep['node_calls'] / max(s0['node_calls'], 1) - 1, 3),
    }
    return {'constants': {'N': N, 'avg_input_tokens': AVG_IN,
                          'output_tokens': OUT,
                          'price_in_per_M': P_IN, 'price_out_per_M': P_OUT,
                          'jev_price_in_per_M': P_JEV_IN},
            'strategies': rows, 'adaptive': adaptive,
            'savings_vs_full_majority': savings}


# ===================================================================
# B. GPU 切分整合（真实 gpu-slicer）
# ===================================================================
def part_b():
    GPU_MEM = 128.0
    workload = {'models': [
        {'name': 'main-chat', 'weights_gb': 42, 'kv_gb': 14,
         'role': 'prefill', 'concurrency': 8},
        {'name': 'small-decode', 'weights_gb': 18, 'kv_gb': 6,
         'role': 'decode', 'concurrency': 4},
        {'name': 'embed-rerank', 'weights_gb': 6, 'kv_gb': 2,
         'role': 'decode', 'concurrency': 2},
        {'name': 'vision', 'weights_gb': 20, 'kv_gb': 8,
         'role': 'prefill', 'concurrency': 2},
    ]}
    total_mem = round(sum(m['weights_gb'] + m['kv_gb']
                          for m in workload['models']), 2)
    plan = slicer.plan(GPU_MEM, workload, mode='auto',
                       supports_mig=False, supports_mps=True)
    usable = plan['usable_gb']
    # 一模型一卡（孤岛现状）
    gpu_naive = len(workload['models'])
    util_naive = round(total_mem / (gpu_naive * GPU_MEM), 3)
    # WNIDIA：本卡容纳；溢出按 usable 增量计卡
    overflow = max(0.0, total_mem - usable)
    gpu_wnidia = 1 + (0 if plan['deferred_models'] == [] and overflow == 0
                      else int(overflow // usable) + 1)
    util_wnidia = round(plan['allocated_gb'] / GPU_MEM, 3)
    return {
        'workload_total_mem_gb': total_mem,
        'usable_per_gpu_gb': usable,
        'naive_one_model_per_gpu': {
            'gpus': gpu_naive, 'gpu_utilization': util_naive},
        'wnidia_sliced': {
            'gpus': gpu_wnidia, 'gpu_utilization': util_wnidia,
            'allocated_gb': plan['allocated_gb'],
            'total_gpu_memory_utilization':
                plan['total_gpu_memory_utilization'],
            'deferred_models': plan['deferred_models'],
            'selected_mode': plan['selected_mode']},
        'gpu_count_reduction_pct': round(
            (gpu_naive - gpu_wnidia) / gpu_naive, 3),
        'utilization_lift_points': round(
            (util_wnidia - util_naive) * 100, 1),
    }


# ===================================================================
# C. 抢占回收（场景假设 *）
# ===================================================================
def part_c():
    events_per_day = 20
    spot_window_min = 30
    remaining_fraction = 0.70
    reclaimed_min = round(events_per_day * spot_window_min
                          * remaining_fraction, 1)
    return {
        'assumptions_star': {
            'preemption_events_per_day': events_per_day,
            'spot_window_min': spot_window_min,
            'avg_remaining_fraction': remaining_fraction},
        'reclaimed_gpu_min_per_day': reclaimed_min,
        'reclaimed_gpu_hours_per_day': round(reclaimed_min / 60, 2),
        'reclaimed_gpu_hours_per_month': round(reclaimed_min / 60 * 30, 1),
    }


# ===================================================================
# D. 闲机纳管（场景假设 *）
# ===================================================================
def part_d():
    idle_nodes = 5
    usable_gb_each = 24
    return {
        'assumptions_star': {'idle_nodes_onboarded': idle_nodes,
                             'usable_gb_each': usable_gb_each},
        'added_usable_capacity_gb': idle_nodes * usable_gb_each,
        'equivalent_dgx_spark_units': round(
            idle_nodes * usable_gb_each / 128, 2),
    }


def main():
    result = {'A_verification_tokens': part_a(),
              'B_gpu_slicing': part_b(),
              'C_preemption_reclaim_star': part_c(),
              'D_idle_onboarding_star': part_d()}
    out = os.path.join(ROOT, 'bench', 'benefit_result.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ---- 控制台摘要 ----
    A = result['A_verification_tokens']
    print('== A 校验 token 经济（每', N, '任务，平均输入', AVG_IN, '）==')
    for name, r in A['strategies'].items():
        print(f"  {name:20} 节点调用 {r['node_calls']:5}  "
              f"折算 ${r['modeled_cost_usd']:7.3f}  检测 {r['detection_coverage']}")
    print('  节省（代表 q=10%）:',
          A['savings_vs_full_majority'])
    B = result['B_gpu_slicing']
    print('\n== B GPU 切分整合 ==')
    print('  一模型一卡:', B['naive_one_model_per_gpu'])
    print('  WNIDIA :', B['wnidia_sliced'])
    print('  GPU 减少:', B['gpu_count_reduction_pct'],
          ' 利用率提升(百分点):', B['utilization_lift_points'])
    print('\n== C 抢占回收 * ==', result['C_preemption_reclaim_star'])
    print('== D 闲机纳管 * ==', result['D_idle_onboarding_star'])
    print('\nJSON ->', out)


if __name__ == '__main__':
    main()
