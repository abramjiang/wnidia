# -*- coding: utf-8 -*-
"""F5 · K8s 接入形态（模拟实现，为真实接入做预备）

现状：WNIDIA 是单控制面 + tmux，非 K8s 生态；生产环境几乎都在 K8s。

本模块提供**与 K8s 对接的适配层**（模拟实现，不依赖 k8s API）：
  1. `parse_node_labels()`：从节点标签解析设备信息 → DeviceProfile 字段
     （真实环境可由 Node Feature Discovery / GPU Operator 提供这些标签）
  2. `admit()`：结合合规策略做准入判断（模拟调度器扩展/准入 webhook 决策）
  3. `prometheus_metrics()`：导出 Prometheus 文本格式指标（补足监控生态）

真实接入时：把 `admit()` 接到调度器扩展或 MutatingWebhook，
把 `prometheus_metrics()` 挂到 /metrics 即可——**业务逻辑无需改动**。

⚠️ 标签名依赖于 NFD / GPU Operator 的实际输出，需实测核对。
"""

from typing import Dict, List, Optional, Tuple

try:
    from .device_profile import DeviceProfile
    from .compliance_policy import CompliancePolicy, evaluate
except ImportError:
    from device_profile import DeviceProfile
    from compliance_policy import CompliancePolicy, evaluate


# K8s 节点标签 → 设备信息（常见标签名，需按实际环境核对）
LABEL_PRODUCT = 'nvidia.com/gpu.product'
LABEL_COUNT = 'nvidia.com/gpu.count'
LABEL_MEMORY = 'nvidia.com/gpu.memory'          # 单位 MiB
LABEL_VENDOR = 'wnidia.io/vendor'               # 自定义：nvidia / amd / intel
LABEL_REGION = 'topology.kubernetes.io/region'
LABEL_TRUSTED = 'wnidia.io/trusted'
LABEL_CC = 'wnidia.io/cc-level'


def _get(labels: Dict[str, str], key: str, default: str = '') -> str:
    return str(labels.get(key, default) or default)


def parse_node_labels(node_name: str,
                      labels: Dict[str, str]) -> Optional[DeviceProfile]:
    """从节点标签解析出 DeviceProfile（无 GPU 标签则返回 None）。"""
    product = _get(labels, LABEL_PRODUCT)
    vendor = _get(labels, LABEL_VENDOR).lower()
    if not product and not vendor:
        return None

    if not vendor:
        vendor = 'nvidia' if product else 'other'

    mem_mib = 0.0
    try:
        mem_mib = float(_get(labels, LABEL_MEMORY, '0'))
    except ValueError:
        mem_mib = 0.0
    capacity_gb = round(mem_mib / 1024.0, 2) if mem_mib else 0.0

    tags = ['k8s']
    if _get(labels, LABEL_TRUSTED).lower() in ('true', '1', 'yes'):
        tags.append('trusted')
    region = _get(labels, LABEL_REGION, 'local')

    return DeviceProfile(
        node_id=node_name,
        gpu_name=product or ('%s-accelerator' % vendor),
        vendor=vendor if vendor in ('nvidia', 'amd', 'intel', 'other') else 'other',
        family='datacenter' if capacity_gb >= 80 else 'consumer',
        capacity_gb=capacity_gb,
        memory_model='discrete',
        interconnect='nvlink' if (vendor == 'nvidia' and capacity_gb >= 80) else 'pcie',
        tags=tags,
    )


def admit(pod_labels: Dict[str, str],
          node: Dict,
          policy: CompliancePolicy = None) -> Dict:
    """准入判断（模拟）：结合合规策略与节点能力。

    pod_labels 需含：wnidia.io/secret-rank（如 L3）、wnidia.io/region
    node 需含：node_id / labels / trusted / cc_level / capacity_gb
    """
    rank = _get(pod_labels, 'wnidia.io/secret-rank', 'L1')
    region = _get(pod_labels, 'wnidia.io/region', 'local')

    node_ctx = {
        'trusted': bool(node.get('trusted')),
        'cc_level': str(node.get('cc_level') or 'CC-L0'),
        'region': str(node.get('region') or 'local'),
        'tags': list(node.get('tags') or []),
    }
    res = evaluate(node_ctx, {'secret_rank': rank, 'region': region}, policy)

    # 容量检查（模拟）：任务需求取自标签 wnidia.io/need-mem-gb
    need = 0.0
    try:
        need = float(_get(pod_labels, 'wnidia.io/need-mem-gb', '0'))
    except ValueError:
        need = 0.0
    cap = float(node.get('capacity_gb') or 0.0)
    if need > 0 and cap > 0 and cap < need:
        res['allowed'] = False
        res['reasons'].append('容量不足：需 %sGB，节点 %sGB' % (need, cap))

    res['node'] = node.get('node_id', '')
    return res


def prometheus_metrics(metrics: List[Dict]) -> str:
    """导出 Prometheus 文本格式（补足监控生态）。

    metrics: [{'node_id','vendor','util_pct','mem_used_gb','mem_total_gb','power_w'}]
    """
    lines = [
        '# HELP wnidia_gpu_util_percent GPU utilization percent',
        '# TYPE wnidia_gpu_util_percent gauge',
    ]
    for m in metrics:
        labels = 'node="%s",vendor="%s"' % (m.get('node_id', ''), m.get('vendor', ''))
        lines.append('wnidia_gpu_util_percent{%s} %s' % (labels, m.get('util_pct', 0)))

    lines += [
        '# HELP wnidia_gpu_memory_used_gb GPU memory used in GB',
        '# TYPE wnidia_gpu_memory_used_gb gauge',
    ]
    for m in metrics:
        labels = 'node="%s",vendor="%s"' % (m.get('node_id', ''), m.get('vendor', ''))
        lines.append('wnidia_gpu_memory_used_gb{%s} %s' % (labels, m.get('mem_used_gb', 0)))

    lines += [
        '# HELP wnidia_gpu_memory_total_gb GPU memory total in GB',
        '# TYPE wnidia_gpu_memory_total_gb gauge',
    ]
    for m in metrics:
        labels = 'node="%s",vendor="%s"' % (m.get('node_id', ''), m.get('vendor', ''))
        lines.append('wnidia_gpu_memory_total_gb{%s} %s' % (labels, m.get('mem_total_gb', 0)))

    lines += [
        '# HELP wnidia_gpu_power_watt GPU power draw in watts',
        '# TYPE wnidia_gpu_power_watt gauge',
    ]
    for m in metrics:
        labels = 'node="%s",vendor="%s"' % (m.get('node_id', ''), m.get('vendor', ''))
        lines.append('wnidia_gpu_power_watt{%s} %s' % (labels, m.get('power_w', 0)))

    return '\n'.join(lines) + '\n'


def _self_test() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    # 解析 NVIDIA 节点标签
    p = parse_node_labels('node-a', {
        LABEL_PRODUCT: 'NVIDIA-H100-80GB-HBM3',
        LABEL_MEMORY: '81920',
        LABEL_VENDOR: 'nvidia',
        LABEL_TRUSTED: 'true',
    })
    check(p is not None, '从标签解析出画像')
    check(p is not None and abs(p.capacity_gb - 80.0) < 0.5, '容量解析正确（80GB）')
    check(p is not None and 'trusted' in p.tags, 'trusted 标签解析正确')
    check(p is not None and p.interconnect == 'nvlink', '大容量 NVIDIA 识别为 nvlink')

    # 无 GPU 标签 → None（不强行建画像）
    check(parse_node_labels('cpu-node', {'foo': 'bar'}) is None,
          '无 GPU 标签返回 None（不误建画像）')

    # 准入：L3 需要 trusted + CC-L2
    secure = {'node_id': 'node-a', 'trusted': True, 'cc_level': 'CC-L3',
              'region': 'local', 'tags': ['trusted'], 'capacity_gb': 80.0}
    r = admit({'wnidia.io/secret-rank': 'L3', 'wnidia.io/need-mem-gb': '40'}, secure)
    check(r['allowed'], 'L3 在安全节点准入通过')

    weak = {'node_id': 'node-b', 'trusted': False, 'cc_level': 'CC-L0',
            'region': 'local', 'tags': [], 'capacity_gb': 80.0}
    r = admit({'wnidia.io/secret-rank': 'L3'}, weak)
    check(not r['allowed'], 'L3 在普通节点被拒')

    r = admit({'wnidia.io/secret-rank': 'L1', 'wnidia.io/need-mem-gb': '200'}, secure)
    check(not r['allowed'] and any('容量不足' in x for x in r['reasons']),
          '容量不足被拒（80GB < 200GB）')

    # Prometheus 导出
    txt = prometheus_metrics([
        {'node_id': 'n1', 'vendor': 'nvidia', 'util_pct': 55.0,
         'mem_used_gb': 30.0, 'mem_total_gb': 80.0, 'power_w': 350.0},
        {'node_id': 'n2', 'vendor': 'amd', 'util_pct': 20.0,
         'mem_used_gb': 10.0, 'mem_total_gb': 192.0, 'power_w': 400.0},
    ])
    check('wnidia_gpu_util_percent{node="n1",vendor="nvidia"} 55.0' in txt,
          'Prometheus 指标格式正确')
    check(txt.count('wnidia_gpu_') >= 8, '导出多类指标（%d 条）' % txt.count('wnidia_gpu_'))
    check(txt.endswith('\n'), '以换行结尾（符合 Prometheus 规范）')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
