# -*- coding: utf-8 -*-
"""F1 · 设备自动发现与画像注册

范围：美股大厂（NVIDIA / AMD / Intel），不含国产卡。

设计要点：
  1. **真实探测与模拟并存** —— 探测命令不存在时优雅降级，
     并可用 `simulate=True` 生成多厂商模拟设备，为真实接入做预备。
  2. **带宽无法从命令行直接取得** —— `nvidia-smi` / `rocm-smi` 都不直接给出显存带宽，
     因此用型号查表（SPEC_TABLE）填充，查不到的记为 0 并在画像里留空，
     **不做猜测**。
  3. 精度支持按架构推导（Blackwell 含 FP4，Ada/Hopper 含 FP8，老架构只到 INT8/FP16）。

⚠️ SPEC_TABLE 中的数值为**参考值，须按官方 datasheet 复核**。
"""

import re
import subprocess
from typing import Dict, List, Optional, Tuple

try:
    from .device_profile import DeviceProfile, DeviceRegistry
except ImportError:
    from device_profile import DeviceProfile, DeviceRegistry


# ---- 型号规格查表（带宽不可从命令行取得，只能查表）----
# ⚠️ 以下为参考值，须按官方 datasheet 复核
SPEC_TABLE: Dict[str, Tuple[float, float, str]] = {
    # gpu_name 关键字 -> (capacity_gb, bandwidth_gb_s, arch)
    'H100': (80.0, 3350.0, 'hopper'),
    'H200': (141.0, 4800.0, 'hopper'),
    'B200': (180.0, 8000.0, 'blackwell'),
    'A100': (80.0, 2039.0, 'ampere'),
    'RTX 5090': (32.0, 1792.0, 'blackwell'),
    'RTX 4090': (24.0, 1008.0, 'ada'),
    'RTX 6000': (48.0, 960.0, 'ada'),
    'GB10': (128.0, 273.0, 'blackwell'),
    'Jetson': (64.0, 204.0, 'ampere'),
    'MI300X': (192.0, 5300.0, 'cdna3'),
    'MI350': (288.0, 8000.0, 'cdna4'),
    'Gaudi': (128.0, 3600.0, 'gaudi'),
}

# 架构 -> 精度支持
ARCH_PRECISION: Dict[str, List[str]] = {
    'blackwell': ['fp4', 'fp8', 'int8', 'fp16'],
    'hopper': ['fp8', 'int8', 'fp16'],
    'ada': ['fp8', 'int8', 'fp16'],
    'ampere': ['int8', 'fp16'],
    'cdna3': ['fp8', 'int8', 'fp16'],
    'cdna4': ['fp4', 'fp8', 'int8', 'fp16'],
    'gaudi': ['fp8', 'int8', 'fp16'],
    'apple': ['fp16', 'int8'],
}


def lookup_spec(gpu_name: str) -> Tuple[float, float, str]:
    """按型号关键字查规格；查不到返回 (0, 0, '')，不做猜测。"""
    for key, val in SPEC_TABLE.items():
        if key.lower() in (gpu_name or '').lower():
            return val
    return (0.0, 0.0, '')


def _run(cmd: List[str]) -> Optional[str]:
    """执行命令；失败或不存在返回 None（优雅降级，不抛异常）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return None


def probe_nvidia() -> List[Dict]:
    """探测 NVIDIA：`nvidia-smi --query-gpu=name,memory.total`。"""
    out = _run(['nvidia-smi',
                '--query-gpu=name,memory.total',
                '--format=csv,noheader,nounits'])
    if not out:
        return []
    devs = []
    for i, line in enumerate(out.splitlines()):
        parts = [p.strip() for p in line.split(',')]
        if not parts:
            continue
        name = parts[0]
        try:
            mem = float(parts[1]) / 1024.0 if len(parts) > 1 else 0.0   # MiB -> GB
        except ValueError:
            mem = 0.0
        devs.append({'vendor': 'nvidia', 'gpu_name': name, 'capacity_gb': round(mem, 1),
                     'index': i})
    return devs


def probe_amd() -> List[Dict]:
    """探测 AMD：尝试 `rocm-smi --showproductname`。"""
    out = _run(['rocm-smi', '--showproductname'])
    if not out:
        return []
    devs = []
    for i, line in enumerate(out.splitlines()):
        if 'GPU' in line and ':' in line:
            name = line.split(':')[-1].strip() or 'AMD GPU'
            devs.append({'vendor': 'amd', 'gpu_name': name, 'capacity_gb': 0.0, 'index': i})
    return devs


def probe_intel() -> List[Dict]:
    """探测 Intel Gaudi：`hl-smi`。无则返回空（不把 Xeon 当加速器）。"""
    out = _run(['hl-smi'])
    if not out:
        return []
    devs = []
    for i, line in enumerate(out.splitlines()):
        if 'Gaudi' in line or 'HL-' in line:
            devs.append({'vendor': 'intel', 'gpu_name': line.strip()[:40],
                         'capacity_gb': 0.0, 'index': i})
    return devs


def simulate_devices() -> List[Dict]:
    """模拟多厂商设备（用于无硬件环境下的验证与预备）。"""
    return [
        {'vendor': 'nvidia', 'gpu_name': 'NVIDIA H100 80GB HBM3', 'capacity_gb': 80.0, 'index': 0},
        {'vendor': 'nvidia', 'gpu_name': 'NVIDIA GeForce RTX 5090', 'capacity_gb': 32.0, 'index': 1},
        {'vendor': 'amd', 'gpu_name': 'AMD Instinct MI300X', 'capacity_gb': 192.0, 'index': 0},
        {'vendor': 'intel', 'gpu_name': 'Intel Gaudi 3', 'capacity_gb': 128.0, 'index': 0},
    ]


def to_profile(d: Dict) -> DeviceProfile:
    """把探测结果转成 DeviceProfile（查表补带宽与精度）。"""
    name = d.get('gpu_name', 'unknown')
    cap_tbl, bw_tbl, arch_tbl = lookup_spec(name)
    capacity = float(d.get('capacity_gb') or 0.0) or cap_tbl
    arch = arch_tbl or ('cdna3' if d.get('vendor') == 'amd'
                        else 'gaudi' if d.get('vendor') == 'intel' else '')
    precision = ARCH_PRECISION.get(arch, [])
    vendor = d.get('vendor', 'other')
    node_id = '%s-%s-%s' % (vendor, arch or 'unknown', d.get('index', 0))

    # 统一内存设备识别（GB10 / Grace / Jetson / Apple 类）
    # ⚠️ 易错点：Apple 芯片名 M1/M2/M3/M4 若用朴素子串匹配，
    #    会把 "HBM3"（H100 的显存标识）误判为 Apple M3 —— 必须按**词边界**匹配。
    up = (name or '').upper()
    is_unified = bool(re.search(r'\b(M1|M2|M3|M4)\b', up)) or (
        any(k in up for k in ('APPLE', 'GB10', 'GRACE', 'JETSON')))
    memory_model = 'unified' if is_unified else 'discrete'

    # 形态判定：先按统一内存分流，再按容量
    if is_unified:
        family = 'soc' if capacity >= 64 else 'edge'
    elif cap_tbl >= 80:
        family = 'datacenter'
    elif cap_tbl and cap_tbl <= 48:
        family = 'consumer'
    else:
        family = 'workstation' if cap_tbl else 'consumer'

    # 互联判定（重要：不能只看架构）
    #   - NVIDIA 数据中心卡才有 NVLink；**消费级 Blackwell(如 RTX 5090) 没有 NVLink**
    #   - 统一内存的一体机(GB10)走片内互连，非 NVLink
    #   - AMD Infinity Fabric 当前未建模，保守记为 pcie
    if vendor == 'nvidia' and family == 'datacenter':
        interconnect = 'nvlink'
    elif is_unified:
        interconnect = 'none'
    else:
        interconnect = 'pcie'

    return DeviceProfile(
        node_id=node_id, gpu_name=name, vendor=vendor, family=family, arch=arch,
        capacity_gb=capacity, memory_model=memory_model, bandwidth_gb_s=bw_tbl,
        compute_tflops={}, precision_support=precision,
        interconnect=interconnect, power_w=0,
        engine_pref=['vllm'] if vendor == 'nvidia' else ['vllm'],
        tags=['auto-discovered'],
    )


def discover(simulate: bool = False) -> List[DeviceProfile]:
    """发现设备并生成画像。

    simulate=True 时返回模拟设备（无硬件环境使用）。
    真实环境下依次探测 NVIDIA / AMD / Intel。
    """
    raw: List[Dict] = []
    if simulate:
        raw = simulate_devices()
    else:
        raw = probe_nvidia() + probe_amd() + probe_intel()
    return [to_profile(d) for d in raw]


def register_discovered(reg: DeviceRegistry, simulate: bool = False) -> Tuple[int, List[str]]:
    """发现并注册；返回 (成功数, 错误列表)。"""
    errs: List[str] = []
    n = 0
    for p in discover(simulate=simulate):
        e = reg.register(p)
        if e:
            errs.extend(e)
        else:
            n += 1
    return n, errs


def _self_test() -> int:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print('  %s %s' % ('PASS' if cond else 'FAIL', msg))
        ok = ok and cond

    # 无硬件时真实探测应优雅返回空列表（不抛异常）
    real = discover(simulate=False)
    check(isinstance(real, list), '真实探测在无工具时不抛异常（返回 %d 个）' % len(real))

    # 模拟发现应产出多厂商画像
    sims = discover(simulate=True)
    check(len(sims) == 4, '模拟发现产出 4 个设备')
    vendors = sorted({p.vendor for p in sims})
    check(vendors == ['amd', 'intel', 'nvidia'], '覆盖三厂商: %s' % vendors)

    h100 = [p for p in sims if 'H100' in p.gpu_name][0]
    check(h100.bandwidth_gb_s == 3350.0, 'H100 带宽查表正确（3350 GB/s）')
    check(h100.capacity_gb == 80.0, 'H100 容量正确（80GB）')
    check('fp8' in h100.precision_support and 'fp4' not in h100.precision_support,
          'Hopper 精度推导正确（含 fp8、不含 fp4）')
    check(h100.interconnect == 'nvlink', 'H100 互联识别为 nvlink')

    rtx = [p for p in sims if '5090' in p.gpu_name][0]
    check('fp4' in rtx.precision_support, 'Blackwell(5090) 含 fp4')
    check(rtx.interconnect == 'pcie',
          '消费级 Blackwell 互联为 pcie（无 NVLink）—— 曾误判为 nvlink')
    check(rtx.family == 'consumer', 'RTX 5090 形态为 consumer')

    # 统一内存一体机：应识别为 unified + soc，且不是 nvlink
    gb = to_profile({'vendor': 'nvidia', 'gpu_name': 'NVIDIA GB10', 'capacity_gb': 128.0})
    check(gb.memory_model == 'unified' and gb.family == 'soc',
          'GB10 识别为统一内存 soc（曾误判为 datacenter）')
    check(gb.interconnect == 'none', 'GB10 无 NVLink 互联')

    amd = [p for p in sims if p.vendor == 'amd'][0]
    check(amd.arch == 'cdna3' and amd.vendor == 'amd', 'AMD 画像正确')

    # 未知型号：不猜测带宽
    unk = to_profile({'vendor': 'nvidia', 'gpu_name': 'Unknown Card XYZ', 'capacity_gb': 16.0})
    check(unk.bandwidth_gb_s == 0.0, '未知型号带宽为 0（不猜测）')
    check(unk.capacity_gb == 16.0, '未知型号仍保留探测到的容量')

    # 注册
    reg = DeviceRegistry()
    n, errs = register_discovered(reg, simulate=True)
    check(n == 4 and not errs, '注册 4 个画像且无错误')

    print('  自检:', 'ALL PASS' if ok else 'HAS FAILURE')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
