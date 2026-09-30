# -*- coding: utf-8 -*-
"""执行器：把任务通过 HTTP 派发给 worker，并回收结果。
统一走共享 httpcli（trust_env=False 禁代理、瞬时失败自动重试）。"""
import os
import requests
from . import config, httpcli
from .models import NodeProfile, TaskSpec


def worker_base(n: NodeProfile) -> str:
    # 本地模式：WNIDIA_WORKER_HOST=127.0.0.1，端口各不同；
    # Docker 模式：不设该变量，用容器名（DNS）解析，内部统一 8100。
    host = os.getenv('WNIDIA_WORKER_HOST') or n.node
    return f'http://{host}:{n.vllm_port}'


def execute(n: NodeProfile, t: TaskSpec) -> dict:
    # v5：与 harness 同一道闸 —— prompt 不可用（隐私占位标记）时不得派发，
    # 否则对端会把 [REDACTED:...] 当真实输入跑出"答案看着正常、其实是假的"。
    from . import prompt_guard
    if prompt_guard.is_redacted(t.prompt) or not t.prompt:
        return {'ok': False, 'error': 'prompt_unavailable'}
    payload = {
        'task': t.task, 'prompt': t.prompt, 'task_type': t.task_type,
        'tokens_in': t.tokens_in, 'progress': t.progress,
        'cheat_seed': t.task,
    }
    r = httpcli.post(f'{worker_base(n)}/execute', json=payload,
                     timeout=config.WORKER_CALL_TIMEOUT)
    return r.json()


def health(n: NodeProfile) -> bool:
    try:
        r = httpcli.get(f'{worker_base(n)}/healthz', timeout=3, retries=1)
        return r.status_code == 200
    except requests.RequestException:
        return False
