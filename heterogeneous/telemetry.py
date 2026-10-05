# -*- coding: utf-8 -*-
"""F2 · 统一遥测适配层

目的：把不同厂商的遥测（NVIDIA DCGM/NVML、AMD ROCm SMI、Intel HL-SMI）
**归一化为统一指标**，使上层（路由/计量/合规）与厂商无关——这是中立性的技术基础。

设计：
  - `UnifiedMetrics`：统一指标结构
  - 各厂商 provider：解析各自命令行输出 → UnifiedMetrics
  - `simulate()`：无硬件时生成模拟指标（为真实接入做预备）
  - 命令不存在时返回 None，**不抛异常**（降级安全）

⚠️ 解析规则依赖命令行输出格式，不同驱动/版本可能有差异，需实测校验。
"""

import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class UnifiedMetrics:
    """统一遥测指标（厂商无关）。"""
    node_id: str = ''
    vendor: str = ''
    util_pct: float = 0.0            # GPU 利用率 0-100
    mem_used_gb: float = 0.0
    mem_total_gb: float = 0.0
    power_w: float = 0.0
    temp_c: float = 0.0
    ts: float = field(default_factory=time.time)
    source: str = 'simulated'        # nvidia | amd | intel | simulated

    def mem_free_gb(self) -> float:
        return max(0.0, self.mem_total_gb - self.mem_used_gb)

    def to_dict(self) -> Dict:
        return asdict(self)


def _run(cmd: List[str]) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return None


def _f(v, default=0.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def nvidia_provider(node_id: str = 'nvidia-0', index: int = 0) -> Optional[UnifiedMetrics]:
    """NVIDIA：`nvidia-smi --query-gpu=... --format=csv,noheader,nounits`"""
    out = _run(['nvidia-smi',
                '--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu',
                '--format=csv,noheader,nounits'])
    if not out:
        return None
    line = out.splitlines()[min(index, len(out.splitlines()) - 1)]
    parts = [p.strip() for p in line.split(',')]
    m = UnifiedMetrics(node_id=node_id, vendor='nvidia', source='nvidia')
    if len(parts) >= 5:
        m.util_pct = _f(parts[0])
        m.mem_used_gb = round(_f(parts[1]) / 1024.0, 2)
        m.mem_total_gb = round(_f(parts[2]) / 1024.0, 2)
        m.power_w = _f(parts[3])
        m.temp_c = _f(parts[4])
    return m


def amd_provider(node_id: str = 'amd-0') -> Optional[UnifiedMetrics]:
    """AMD：`rocm-smi --showuse --showmemuse --showpower --showtemp`（格式因版本而异）。"""
    out = _run(['rocm-smi', '--showuse', '--showmemuse', '--showpower', '--showtemp'])
    if not out:
        return None
    m = UnifiedMetrics(node_id=node_id, vendor='amd', source='amd')
    kv: Dict[str, str] = {}
    for line in out.splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            kv[k.strip().upper()] = v.strip()
    m.util_pct = _f(kv.get('GPU USE (%)', '0'))
    m.power_w = _f(kv.get('CURRENT SOCKET POWER (W)', '0'))
    m.temp_c = _f(kv.get('TEMPERATURE (EDGE) (C)', '0'))
    return m


def intel_provider(node_id: str = 'intel-0') -> Optional[UnifiedMetrics]:
    """Intel Gaudi：`hl-smi`（输出解析较粗，需实测校准）。"""
    out = _run(['hl-smi'])
    if not out:
        return None
    m = UnifiedMetrics(node_id=node_id, vendor='intel', source='intel')
    txt = out.upper()
    if 'UTIL' in txt:
        for line in out.splitlines():
            if '%' in line:
                try:
                    m.util_pct = float(line.split('%')[0].split()[-1])
                    break
                except (IndexError, ValueError):
                    continue
    return m


def simulate_provider(node_id: str = 'sim-0', vendor: str = 'nvidia',
                      util_pct: float = 55.0, mem_total_gb: float = 80.0,
                      mem_used_gb: float = 32.0, power_w: float = 350.0,
                      temp_c: float = 62.0) -> UnifiedMetrics:
    """模拟指标（无硬件环境使用，为真实接入做预备）。"""
    return UnifiedMetrics(node_id=node_id, vendor=vendor, util_pct=util_pct,
                          mem_used_gb=mem_used_gb, mem_total_gb=mem_total_gb,
                          power_w=power_w, temp_c=temp_c, source='simulated')


def collect(node_id: str = 'node-0', vendor: str = 'nvidia',
            simulate: bool = False, **kw) -> UnifiedMetrics:
    """采集指标；真实 provider 失败时**自动降级为模拟**，保证上层不中断。"""
    if not simulate:
        get = {'nvidia': nvidia_provider,
               'amd': amd_provider,
               'intel': intel_provider}.get(vendor)
        if get:
            m = get(node_id)
            if m is not None:
                return m
    return simulate_provider(node_id=node_id, vendor=vendor, **kw)


def collect_all(devices: List[Dict], simulate: bool = True) -> List[UnifiedMetrics]:
    """批量采集：devices 为 [{'node_id','vendor', ...}]。"""
    out = []
    for d in devices:
        vendor = d.get('vendor', 'nvidia')
        node_id = d.get('node_id', '%s-0' % vendor)
        if simulate:
            out.append(simulate_provider(
                node_id=node_id, vendor=vendor,
                mem_total_gb=float(d.get('capacity_gb') or 0.0),
                mem_used_gb=float(d.get('mem_used_gb') or 0.0)))
        else:
            out.append(collect(node_id=node_id, vendor=vendor, simulate=False))
    return out


def _self_test() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    # 无硬件：真实 provider 返回 None，collect 降级为模拟且不抛异常
    m = collect(node_id='n0', vendor='nvidia', simulate=False)
    check(m is not None and m.source == 'simulated',
          '命令缺失时优雅降级为模拟（source=%s）' % m.source)

    s = simulate_provider(node_id='h100', mem_total_gb=80.0, mem_used_gb=30.0)
    check(abs(s.mem_free_gb() - 50.0) < 1e-6, '剩余容量计算正确（50GB）')
    check(s.to_dict()['vendor'] == 'nvidia', '序列化正确')

    devs = [{'node_id': 'a', 'vendor': 'nvidia', 'capacity_gb': 80.0},
            {'node_id': 'b', 'vendor': 'amd', 'capacity_gb': 192.0},
            {'node_id': 'c', 'vendor': 'intel', 'capacity_gb': 128.0}]
    ms = collect_all(devs, simulate=True)
    check(len(ms) == 3 and {m.vendor for m in ms} == {'nvidia', 'amd', 'intel'},
          '批量采集覆盖三厂商')

    # 未知厂商不应崩溃
    u = collect(node_id='x', vendor='unknown-vendor', simulate=True)
    check(u is not None, '未知厂商不崩溃')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
