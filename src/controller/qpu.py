# -*- coding: utf-8 -*-
"""QPU 资源抽象与混合任务接口（BP P18 远期期权 · 只做接口预留与沙盒验证）。

**能力边界（必须如实标注）**
    - 本模块是**纯 Python 的 statevector 模拟器**，用于验证"调度器能把量子任务
      当作一类资源纳管"这条接口链路，**不代表项目具备量子计算能力**。
    - 真机路径：BP 原文要求「以 CUDA-Q 类框架接入」。本模块预先声明
      `backend='cudaq'` 的接口位；未安装 CUDA-Q 时**明确拒绝**，不静默回退到模拟。
    - 支持门集刻意收紧（x / h / ry / rz / cx / measure），只用于演示电路。

设计借鉴：Qiskit 与 Cirq 的 statevector 模拟器「门序列 → 振幅向量 → 采样」
三步结构；本项目不引入任何量子依赖，保持零新增第三方包。
"""
import cmath
import hashlib
import math
import os
import random

MAX_QUBITS = int(os.getenv('WNIDIA_QPU_MAX_QUBITS', '12'))

# 内建模拟后端
SIM_BACKEND = 'sim-statevector'
# 真机/框架后端（接口位，未接入）
CUDAQ_BACKEND = 'cudaq'

KNOWN_GATES = ('x', 'h', 'ry', 'rz', 'cx', 'measure')


def backends():
    return {
        SIM_BACKEND: {'available': True, 'kind': 'simulator',
                      'note': '内建 statevector 模拟，仅用于接口链路验证'},
        CUDAQ_BACKEND: {'available': _cudaq_available(), 'kind': 'framework',
                        'note': 'NVIDIA CUDA-Q（GPU 加速量子模拟）；'
                                '未安装时拒绝执行，不回退模拟'},
        'qpu-hardware': {'available': False, 'kind': 'hardware',
                         'note': '容错量子硬件尚未成熟；按 BP 口径仅做跟踪与接口预留'},
    }


def _cudaq_available():
    try:
        import cudaq  # noqa: F401
        return True
    except Exception:                      # noqa: BLE001
        return False


# ---------------------------------------------------------------- statevector 模拟
def _zero_state(n):
    return [complex(1, 0)] + [complex(0, 0)] * ((1 << n) - 1)


def _apply_single(state, n, q, mat):
    """对第 q 个量子位施加 2x2 矩阵。"""
    step = 1 << q
    for base in range(0, 1 << n, step << 1):
        for off in range(step):
            i0 = base + off
            i1 = i0 + step
            a, b = state[i0], state[i1]
            state[i0] = mat[0][0] * a + mat[0][1] * b
            state[i1] = mat[1][0] * a + mat[1][1] * b


def _apply_cx(state, n, ctrl, targ):
    if ctrl == targ:
        raise ValueError('cx 的控制位与目标位不能相同')
    for i in range(1 << n):
        if (i >> ctrl) & 1 and not (i >> targ) & 1:
            j = i | (1 << targ)
            state[i], state[j] = state[j], state[i]


GATE_MATRIX = {
    'x': [[0, 1], [1, 0]],
    'h': [[1 / math.sqrt(2), 1 / math.sqrt(2)],
          [1 / math.sqrt(2), -1 / math.sqrt(2)]],
}


def _ry(theta):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return [[c, -s], [s, c]]


def _rz(theta):
    return [[cmath.exp(-1j * theta / 2), 0], [0, cmath.exp(1j * theta / 2)]]


def run_circuit(circuit, qubits, shots=1024, seed=1234):
    """执行受限门集的电路。

    circuit: [{'gate':'h','q':0}, {'gate':'cx','c':0,'t':1},
              {'gate':'ry','q':0,'theta':1.5708}, {'gate':'measure','q':0}]
    返回 {'counts': {...}, 'probabilities': [...], 'statevector_digest': '...'}
    """
    if not isinstance(qubits, int) or qubits < 1:
        raise ValueError('qubits 必须是正整数')
    if qubits > MAX_QUBITS:
        raise ValueError(f'qubits 超过模拟上限 {MAX_QUBITS}（statevector 内存限制）')
    if not isinstance(circuit, list) or not circuit:
        raise ValueError('circuit 必须是非空门序列')
    shots = int(shots)
    if shots < 1 or shots > 100000:
        raise ValueError('shots 需在 1..100000')

    n = qubits
    state = _zero_state(n)
    measured = set()
    for idx, g in enumerate(circuit):
        if not isinstance(g, dict):
            raise ValueError(f'第 {idx} 个门不是对象')
        name = str(g.get('gate') or '').lower()
        if name not in KNOWN_GATES:
            raise ValueError(f'第 {idx} 个门 {name!r} 不在支持集合 {KNOWN_GATES}')
        if name == 'cx':
            c, t = int(g.get('c', 0)), int(g.get('t', 1))
            _check_qbit(c, n, 'c'); _check_qbit(t, n, 't')
            _apply_cx(state, n, c, t)
        elif name == 'measure':
            q = int(g.get('q', 0))
            _check_qbit(q, n, 'q')
            measured.add(q)
        else:
            q = int(g.get('q', 0))
            _check_qbit(q, n, 'q')
            if name == 'ry':
                _apply_single(state, n, q, _ry(float(g.get('theta', 0.0))))
            elif name == 'rz':
                _apply_single(state, n, q, _rz(float(g.get('theta', 0.0))))
            else:
                _apply_single(state, n, q, GATE_MATRIX[name])

    probs = [abs(a) ** 2 for a in state]
    total = sum(probs) or 1.0
    probs = [p / total for p in probs]

    rnd = random.Random(seed)
    counts = {}
    for _ in range(shots):
        r, acc, pick = rnd.random(), 0.0, len(probs) - 1
        for i, p in enumerate(probs):
            acc += p
            if r <= acc:
                pick = i
                break
        key = format(pick, f'0{n}b')[::-1]
        if measured:
            key = ''.join(key[q] for q in sorted(measured))
        counts[key] = counts.get(key, 0) + 1

    digest = hashlib.sha256(
        '|'.join(f'{i}:{probs[i]:.12f}' for i in range(len(probs))).encode(
            'utf-8')).hexdigest()
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    return {'counts': dict(top), 'probabilities': [round(p, 6) for p in probs],
            'statevector_digest': digest, 'shots': shots,
            'measured_qubits': sorted(measured), 'qubits': n}


def _check_qbit(q, n, field):
    if not (0 <= q < n):
        raise ValueError(f'{field}={q} 超出量子位数 0..{n - 1}')


# ---------------------------------------------------------------- 任务接口
def submit(job_id, circuit, qubits=2, shots=1024, backend=SIM_BACKEND,
           seed=1234, note=''):
    """提交一个量子任务（混合任务接口的入口）。"""
    from . import db
    if backend == CUDAQ_BACKEND and not _cudaq_available():
        return {'ok': False, 'backend': backend,
                'reason': 'CUDA-Q 未安装；按 BP 口径此处只做接口预留，'
                          '不回退到内建模拟（避免假装已接入）'}
    if backend not in (SIM_BACKEND, CUDAQ_BACKEND):
        return {'ok': False, 'backend': backend,
                'reason': f'未知后端；可选 {SIM_BACKEND} / {CUDAQ_BACKEND}'}
    # BUG-V4-17：circuit 来自请求体，原实现只捕获 ValueError/TypeError。
    # 若传入字符串或含非 dict 元素，会在 run_circuit 内抛 AttributeError → HTTP 500。
    # 先做形状校验，保证"脏输入 = 明确拒绝"而不是 500。
    if not isinstance(circuit, list) or not all(
            isinstance(g, dict) for g in circuit):
        return {'ok': False, 'backend': backend,
                'reason': 'circuit 必须是门对象数组，例如 '
                          '[{"gate":"h","q":0},{"gate":"measure","q":0}]'}
    try:
        res = run_circuit(circuit, qubits, shots, seed)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
        db.add_qpu_job(job_id, backend, qubits if isinstance(qubits, int) else 0,
                       0, str(circuit)[:200], {}, 'failed', note=str(e)[:120])
        return {'ok': False, 'backend': backend, 'reason': str(e)}

    db.add_qpu_job(job_id, backend, qubits, shots, str(circuit)[:500], res,
                   'done', note=note or '沙盒模拟执行')
    from . import trust
    trust.seal('qpu-job', job_id, res['statevector_digest'])
    db.add_event('qpu',
                 f'量子任务 {job_id} 完成（{backend}，{qubits} qubits，'
                 f'{shots} shots，digest={res["statevector_digest"][:12]}…）')
    return {'ok': True, 'job': job_id, 'backend': backend,
            'simulated': backend == SIM_BACKEND, 'result': res,
            'note': ('内建 statevector 模拟结果，非量子硬件；'
                     '真机路径需接入 CUDA-Q')}


def status():
    from . import db
    rows = db.qpu_job_rows(20)
    return {'backends': backends(), 'max_qubits': MAX_QUBITS,
            'gates': list(KNOWN_GATES), 'recent_jobs': rows,
            'capability_note': ('本模块为接口预留 + 沙盒验证：调度器可把 QPU 当作'
                                '一类资源纳管，但项目不宣称具备量子计算能力。')}


# ---------------------------------------------------------------- PQC 预留
def pqc_status():
    return {
        'ready': False,
        'interface': ['pqc_sign(payload)', 'pqc_verify(payload, signature)'],
        'current_impl': 'HMAC-SHA256 占位（仅链路演示，不是后量子算法）',
        'real_path': '接入 liboqs（ML-KEM / ML-DSA，NIST PQC 标准）',
        'note': ('按 BP P18 口径：机密度计算层预留后量子密码升级路径，'
                 '保护今天加密的数据不被未来破解；当前未实现真实 PQC。'),
    }


def pqc_sign(payload):
    """占位实现：明确标注非 PQC。真实升级点在此替换为 liboqs。"""
    from . import trust
    return 'hmac-sha256-placeholder:' + trust.kms_sign(str(payload))


def pqc_verify(payload, signature):
    from . import trust
    if not isinstance(signature, str) or not signature.startswith(
            'hmac-sha256-placeholder:'):
        return False
    return trust.kms_verify(str(payload),
                            signature.split(':', 1)[1])
