# -*- coding: utf-8 -*-
"""GPU Slicer：确定性计算 GPU 切分方案与 vLLM 启动命令。"""
import argparse
import json

RESERVE = 0.08          # 预留 8% 给 CUDA context / 碎片
BASE_PORT = 8100
# 引擎端口不是公网映射端口（手册 4.1：只有 8888/9000 被映射），
# 绑 0.0.0.0 会把推理服务暴露给同网段的其他参赛节点 → 一律绑回环。
ENGINE_BIND = '127.0.0.1'


def _instance(idx, m, gpu_mem, port):
    mem = round(m['weights_gb'] + m['kv_gb'], 2)
    util = round(mem / gpu_mem, 3)
    cmd = (f"vllm serve {m['name']} --host {ENGINE_BIND} --port {port} "
           f"--gpu_memory_utilization {util} --tensor-parallel-size 1 "
           f"--max-model-len {m.get('max_model_len', 32768)}")
    return {
        'id': f'inst-{idx}', 'name': m['name'], 'mem_gb': mem,
        'gpu_memory_utilization': util,
        'role': m.get('role', 'decode'), 'concurrency': m.get('concurrency', 1),
        'port': port, 'serve_command': cmd,
        # 对外访问方式：控制面 :9000 统一鉴权代理；其余端口走 SSH 隧道
        'access_hint': (f'只监听 {ENGINE_BIND}:{port}。本机调用直连；'
                        f'外部请用 ssh -p 6NN -L {port}:localhost:{port} '
                        f'Developer@<公网IP> 建隧道（手册第五章）'),
    }


def plan(gpu_mem, workload, mode='auto', supports_mig=False, supports_mps=True):
    models = workload.get('models', [])
    usable = round(gpu_mem * (1 - RESERVE), 2)

    # MIG：GB10 默认不支持
    if mode == 'mig' and not supports_mig:
        mode = 'auto'; mig_note = 'MIG 不可用（GB10 不支持/未授权），已回退'
    else:
        mig_note = ''

    instances, deferred, used = [], [], 0.0
    small = any((m['weights_gb'] + m['kv_gb']) <= gpu_mem * 0.25 for m in models)
    for i, m in enumerate(models):
        need = m['weights_gb'] + m['kv_gb']
        if used + need <= usable:
            instances.append(_instance(i, m, gpu_mem, BASE_PORT + i))
            used += need
        else:
            deferred.append(m['name'])

    # 选型
    if mode == 'mig' and supports_mig:
        selected = 'mig'
    elif mode == 'mps':
        selected = 'mps'
    elif mode == 'instances':
        selected = 'instances'
    else:  # auto：保底多实例；小负载且支持 MPS 时叠加
        selected = 'mps' if (small and supports_mps and not deferred) else 'instances'

    mps_on = selected == 'mps'
    total_util = round(sum(x['gpu_memory_utilization'] for x in instances), 3)
    return {
        'selected_mode': selected,
        'gpu_mem_gb': gpu_mem, 'usable_gb': usable, 'allocated_gb': round(used, 2),
        'total_gpu_memory_utilization': total_util,
        'instances': instances,
        'mps': {'enabled': mps_on,
                'note': (mig_note + '；' if mig_note else '') +
                        ('用户态共享，失败自动回退多实例' if mps_on else
                         '未启用 MPS，使用多实例保底')},
        'accounting': {
            x['id']: {'mem_gb': x['mem_gb'], 'concurrency': x['concurrency'],
                      'gpu_memory_utilization': x['gpu_memory_utilization']}
            for x in instances},
        'deferred_models': deferred,
        'fallback': [
            'MPS 异常 → 关闭 MPS，回到多 vLLM 实例',
            '显存超分 → 降低 KV/并发或分时加载，deferred 模型排队',
            '实例失败 → smart-dispatcher 重路由到健康实例',
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gpu-mem', type=float, default=128)
    ap.add_argument('--workload', default='{"models":[]}')
    ap.add_argument('--workload-file', default=None,
                    help='从 JSON 文件读取 workload（离线/评测用）')
    ap.add_argument('--mode', default='auto')
    ap.add_argument('--supports-mig', action='store_true')
    ap.add_argument('--no-mps', action='store_true')
    a = ap.parse_args()
    if a.workload_file:
        with open(a.workload_file, encoding='utf-8') as f:
            wl = json.load(f)
    else:
        wl = json.loads(a.workload)
    print(json.dumps(plan(a.gpu_mem, wl, a.mode, a.supports_mig,
                          not a.no_mps), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
