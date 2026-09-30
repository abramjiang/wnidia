# -*- coding: utf-8 -*-
"""模拟计算后端：无 GPU 时提供确定性的“推理”结果与可中断的长任务。
所有数字均为模拟，演示中明确标注，不冒充真机数据。"""
import hashlib
import time

CANNED = [
    '已完成调度：该任务按 SLA 与密级匹配到最合适的算力节点。',
    'WNIDIA 已把任务分发到当前利用率最低、显存充足的实例。',
    '结果已通过校验并回流，节点信誉分与调度权重已更新。',
]


def _seed(text):
    return int(hashlib.md5(text.encode('utf-8')).hexdigest()[:8], 16)


def one_shot(prompt: str, tokens_in: int = 128, cheat: bool = False) -> str:
    if cheat:
        # 作弊节点返回错误/篡改结果
        return '【错误结果】该任务无需调度，所有 GPU 均不可用（模拟作弊节点输出）'
    h = _seed(prompt)
    return CANNED[h % len(CANNED)] + f'（模拟推理，输入约 {tokens_in} tokens）'


def est_duration_s(task_type: str, tokens_in: int) -> float:
    base = 0.4 + min(tokens_in, 4000) / 4000.0 * 1.2
    if task_type == 'batch':
        return base + 6.0       # 批处理任务更长，便于演示抢占
    if task_type == 'heavy':
        return base + 2.0
    return base


def utilization_curve(elapsed: float, duration: float, compute_pct: int):
    """模拟利用率：爬坡 → 高位 → 收尾。"""
    if duration <= 0:
        return 0.0
    r = min(max(elapsed / duration, 0), 1)
    if r < 0.15:
        u = r / 0.15
    elif r > 0.9:
        u = max((1 - r) / 0.1, 0)
    else:
        u = 1.0
    return round(u * compute_pct, 1)
