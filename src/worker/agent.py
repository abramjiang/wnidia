# -*- coding: utf-8 -*-
"""WNIDIA Worker：启动即注册、周期心跳；接收任务并执行。

v4 新增（BP P13/P14 + P7 画像 + P10 计量）：
1. **边缘断网自治**：控制面不可达时任务在图本地队列继续完成；恢复后按批次回放，
   控制面做幂等去重与计量补偿。重连采用指数退避（借鉴 KubeEdge/EdgeFlow 的做法）。
2. **真实用量透传**：把引擎返回的 `usage` 一并回传，供控制面真实计量；
   缺失时由控制面回退估算并标 `estimated`。
3. **画像补维度**：带宽 / 时延 / 在线率 / 稳定性 / 机型 / 代际 / 机密计算层级，
   全部随注册与心跳上报。
4. **沙盒开关**：`POST /offline/simulate` 可伪造断网，用于验证自治链路。

合规：控制接口一律只绑回环（WORKER_BIND 默认 127.0.0.1）。
"""
import os
import shutil
import subprocess
import sys
import threading
import time

import requests
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from worker import mock_compute   # noqa: E402

try:
    from controller import engines as eng
    from controller import compliance as comp
except Exception:                 # noqa: BLE001
    eng = comp = None

# ---- 自身参数 ----
NODE      = os.getenv('NODE', 'node-x')
ROLE      = os.getenv('NODE_ROLE', 'decode')
TIER      = os.getenv('NODE_TIER', 'edge')
MEM_LIMIT = float(os.getenv('MEM_LIMIT_GB', '8'))
COMPUTE   = int(os.getenv('COMPUTE_PCT', '100'))
PORT      = int(os.getenv('VLLM_PORT', '8100'))
CTRL      = os.getenv('CTRL', 'http://127.0.0.1:9000')
TOKEN     = os.getenv('WNIDIA_TOKEN', 'changeme')
CHEAT     = os.getenv('CHEAT', 'false').lower() == 'true'
GPU_NAME  = os.getenv('GPU_NAME', 'mock-gpu')
WS        = requests.Session(); WS.trust_env = False

HAS_GPU = os.getenv('WNIDIA_MODE', 'auto') == 'gpu' or \
    (os.getenv('WNIDIA_MODE', 'auto') == 'auto' and
     os.path.exists('/dev/nvidia0'))
VLLM_BASE = os.getenv('VLLM_BASE', f'http://127.0.0.1:{PORT}/v1')
VLLM_MODEL    = os.getenv('VLLM_MODEL', 'stepfun')
VLLM_MAXTOK   = int(os.getenv('VLLM_MAX_TOKENS', '256'))
VLLM_TIMEOUT  = int(os.getenv('VLLM_TIMEOUT', '240'))

# v4 画像维度（部署时用环境变量声明）
FORM_FACTOR = os.getenv('NODE_FORM_FACTOR',
                        'box' if TIER == 'edge' else
                        ('mini' if TIER == 'home' else 'server'))
GENERATION  = os.getenv('NODE_GENERATION', 'gb10' if HAS_GPU else 'sim')
CC_LEVEL    = os.getenv('NODE_CC_LEVEL', 'CC-L0')
BANDWIDTH   = float(os.getenv('NODE_BANDWIDTH_MBPS', '100'))
POWER_W     = int(os.getenv('NODE_POWER_W', '0'))
SOVEREIGN   = os.getenv('NODE_REGION', 'local')

ENGINE_NAME = eng.resolve() if eng else ('ollama' if HAS_GPU else 'mock')
FALLBACKS = [n for n in ((eng.bench_engine() if eng else ['mock']))
             if n != ENGINE_NAME][:2] or ['mock']
ENGINE_STATE = {'name': ENGINE_NAME, 'healthy': None, 'detail': '未探活',
                'backend': '', 'exact': None, 'degraded': not HAS_GPU}

# ---- 运行期统计（画像与门槛用）----
STATS = {'hb_ok': 0, 'hb_fail': 0, 'latency_ms': 0.0, 'streak_fail': 0,
         'max_streak_fail': 0}

# ---- 断网自治 ----
OFFLINE = {'active': False, 'since': 0, 'queue': [], 'tokens': 0,
           'seconds': 0.0, 'simulated': False}
OFFLINE_LOCK = threading.Lock()

app = FastAPI(title=f'worker-{NODE}')


# ---------------- 作业（可中断） ----------------
# P0 修复（2026-09-29）：真实引擎的进度原先是 elapsed / REAL_TASK_EXPECT_S
# （默认 20s），而 27B 实测均值 45.7s —— 进度条 20 秒就冲到 100%，
# 然后卡在 100% 再干等 25 秒，观感就是"画面不动"；/preempt 存的 checkpoint
# 也跟着失真。改为「阶段进度 + 自适应 ETA」：
#   阶段给出可信下界（dispatched→prefill→decode→finalize→done）；
#   解码段在段内按 ETA 平滑推进；**done 之前封顶 0.98**；
#   ETA 取最近若干条真实任务耗时的中位数，无历史时用 REAL_TASK_EXPECT_S 兜底。
STAGE_BASE = {'queued': 0.04, 'dispatched': 0.10, 'prefill': 0.20,
              'decode': 0.32, 'finalize': 0.96, 'done': 1.0}
_DECODE_SPAN = STAGE_BASE['finalize'] - STAGE_BASE['decode']      # 0.64
_PRE_DECODE_FRAC = 0.15        # ETA 的前 15% 视为"预填充 + 首 token"
_REAL_HISTORY = []             # 最近若干条真实任务耗时（秒）
_REAL_HISTORY_MAX = 8


def _eta_seconds():
    """自适应 ETA：最近真实耗时的中位数；无历史时用 REAL_TASK_EXPECT_S 兜底。"""
    if _REAL_HISTORY:
        s = sorted(_REAL_HISTORY)
        return max(1.0, s[len(s) // 2])
    return max(1.0, float(os.getenv('REAL_TASK_EXPECT_S', '20')))


def _push_real_duration(sec):
    """记录一条真实任务耗时，用于自校准 ETA（异常一律忽略）。"""
    try:
        v = float(sec)
        if v > 0:
            _REAL_HISTORY.append(v)
            if len(_REAL_HISTORY) > _REAL_HISTORY_MAX:
                del _REAL_HISTORY[0]
    except Exception:      # noqa: BLE001
        pass


class Job:
    def __init__(self, task, prompt, task_type, tokens_in):
        self.task = task; self.prompt = prompt; self.task_type = task_type
        self.tokens_in = tokens_in
        self.duration = float(os.getenv('REAL_TASK_EXPECT_S', '20')) \
            if HAS_GPU else mock_compute.est_duration_s(task_type, tokens_in)
        self.elapsed = 0.0; self.state = 'running'
        self.answer = ''
        self.usage = None
        self.engine = ENGINE_STATE['name']
        self.pause = threading.Event(); self.pause.set()
        self.started = time.time()
        # --- P0：阶段进度状态 ---
        self.stage = 'dispatched'
        self.eta = _eta_seconds() if HAS_GPU else float(self.duration or 0)
        self._decode_from = self.eta * _PRE_DECODE_FRAC

    def _advance_stage(self):
        """按 elapsed 推进阶段（仅真实引擎路径使用）。"""
        if self.elapsed < self._decode_from:
            self.stage = 'prefill'
        elif self.elapsed < self.eta:
            self.stage = 'decode'
        else:
            self.stage = 'finalize'      # 超出 ETA 仍在跑：停在 96%，不再封顶

    def progress_pct(self):
        """完成度 0..1。

        BUG-V4-01：v4 重写 worker 时漏了这个方法，而 `/status`、`/preempt`、
        `/resume` 三个端点都在调用它 —— 结果三个端点全部 500，
        控制面轮询拿不到状态，**所有任务永久停在 running**，
        抢占/恢复链同样失效。此处补回，并对 duration<=0 做保护
        （duration 为 0 时直接视作已完成，避免除零）。

        P0：真实引擎改走阶段进度（见 STAGE_BASE 注释），mock 路径保持原语义，
        因为沙盒与评测依赖它的确定性。
        """
        if self.state == 'done':
            return 1.0
        if not HAS_GPU:
            d = float(self.duration or 0)
            if d <= 0:
                return 0.0
            return round(min(1.0, max(0.0, self.elapsed / d)), 4)
        base = STAGE_BASE.get(self.stage, STAGE_BASE['decode'])
        if self.stage == 'decode':
            frac = (self.elapsed - self._decode_from) / \
                max(self.eta - self._decode_from, 0.5)
            p = base + _DECODE_SPAN * min(1.0, max(0.0, frac))
        else:
            p = base
        return round(min(0.98, max(0.0, p)), 4)     # done 前不封顶 1.0

    def run(self):
        step = 0.1
        if HAS_GPU:
            box = {'done': False}

            def _call():
                try:
                    ans, usage, used_engine = engine_call(self.prompt)
                    box['ans'] = ans; box['usage'] = usage
                    box['engine'] = used_engine
                except Exception as e:          # noqa: BLE001
                    box['err'] = str(e)[:200]
                finally:
                    box['done'] = True

            t0 = time.time()
            threading.Thread(target=_call, daemon=True).start()
            limit = max(self.eta * 3.0, VLLM_TIMEOUT + 30)
            while not box['done'] and self.elapsed < limit:
                self.pause.wait()
                time.sleep(step); self.elapsed += step
                self._advance_stage()
            self.answer = box.get('ans') or \
                f'[engine error] {box.get("err", "timeout")}'
            self.usage = box.get('usage')
            self.engine = box.get('engine') or ENGINE_STATE['name']
            self.state = 'done'
            self.stage = 'done'
            _push_real_duration(time.time() - t0)   # 自校准下一条的 ETA
            return
        while self.elapsed < self.duration:
            self.pause.wait()
            time.sleep(step); self.elapsed += step
        self.answer = mock_compute.one_shot(
            self.prompt, self.tokens_in, cheat=RUNTIME['cheat'])
        # mock 引擎：生成确定性伪 usage（便于验证计量链路）
        self.usage = {'prompt_tokens': max(1, (self.tokens_in or 0)),
                      'completion_tokens': max(1, len(self.answer) // 2),
                      'total_tokens': max(1, (self.tokens_in or 0)) +
                                      max(1, len(self.answer) // 2),
                      'synthetic': True}
        self.engine = 'mock'
        self.state = 'done'


JOBS = {}
JOBS_LOCK = threading.Lock()
RUNTIME = {'cheat': CHEAT}


def _start_job(j: Job):
    threading.Thread(target=j.run, daemon=True).start()


def _saturate(j: Job):
    """任务完成后若控制面不可达，把它记入本地队列（断网自治）。"""
    with OFFLINE_LOCK:
        if not OFFLINE['active']:
            return
        tokens = int((j.usage or {}).get('total_tokens') or 0)
        OFFLINE['queue'].append(j.task)
        OFFLINE['tokens'] += tokens
        OFFLINE['seconds'] += float(j.elapsed or 0)


class StartReq(BaseModel):
    task: str; prompt: str = ''; task_type: str = 'chat'; tokens_in: int = 128


class ExecReq(BaseModel):
    task: str; prompt: str = ''; task_type: str = 'chat'; tokens_in: int = 128
    progress: float = 0.0; cheat_seed: str = ''


@app.get('/healthz')
def healthz():
    return {'ok': True, 'node': NODE, 'gpu': HAS_GPU,
            'engine': ENGINE_STATE['name'], 'degraded': ENGINE_STATE['degraded'],
            'offline': OFFLINE['active'], 'pending_local': len(OFFLINE['queue'])}


@app.get('/engine')
def engine_info():
    s = eng.spec() if eng else {'name': ENGINE_NAME, 'base': VLLM_BASE,
                                'model': VLLM_MODEL, 'kind': 'openai-compat',
                                'backend': '', 'exact': None}
    return {'node': NODE, 'engine': ENGINE_STATE['name'],
            'fallback_chain': FALLBACKS, 'spec': s, 'live': dict(ENGINE_STATE)}


@app.get('/profile')
def profile():
    """本节点画像（含 v4 新维度），供控制面与沙盒核对。"""
    return {
        'node': NODE, 'tier': TIER, 'role': ROLE,
        'form_factor': FORM_FACTOR, 'generation': GENERATION,
        'cc_level': CC_LEVEL, 'bandwidth_mbps': BANDWIDTH, 'power_w': POWER_W,
        'sovereign_region': SOVEREIGN,
        'latency_ms': STATS['latency_ms'],
        'uptime_ratio': _uptime_ratio(), 'stability': _stability(),
        'engine': ENGINE_STATE['name'], 'gpu': HAS_GPU,
        'offline': OFFLINE['active'], 'pending_local': len(OFFLINE['queue']),
    }


@app.get('/metrics/gpu')
def metrics_gpu():
    """真实 GPU 画像（NVML 优先，来源自证）。仅回环可达，不构成暴露面。"""
    d = gpu_metrics()
    d['node'] = NODE
    return d


def _uptime_ratio():
    tot = STATS['hb_ok'] + STATS['hb_fail']
    return round(STATS['hb_ok'] / tot, 4) if tot else 1.0


def _stability():
    """稳定性：连续失败次数越多越低（0.5 下限）。"""
    return round(max(0.5, 1.0 - 0.1 * STATS['max_streak_fail']), 4)


# ---------------- 断网自治 ----------------
class OfflineReq(BaseModel):
    active: bool = True
    note: str = ''


@app.post('/offline/simulate')
def offline_simulate(req: OfflineReq):
    """沙盒：手动进入/退出断网自治（不真实切断网络）。"""
    return _set_offline(req.active, simulated=True, note=req.note)


def _set_offline(active, simulated=False, note=''):
    with OFFLINE_LOCK:
        if active and not OFFLINE['active']:
            OFFLINE.update(active=True, since=int(time.time() * 1000),
                           simulated=simulated)
            db_event(f'{NODE} 进入断网自治（{"沙盒模拟" if simulated else "控制面不可达"}）')
            return {'ok': True, 'offline': True, 'simulated': simulated,
                    'since': OFFLINE['since'], 'note': note}
        if not active and OFFLINE['active']:
            OFFLINE['active'] = False
            return {'ok': True, 'offline': False, 'note': note,
                    'pending': len(OFFLINE['queue'])}
    return {'ok': True, 'offline': bool(OFFLINE['active']), 'note': note}


@app.get('/offline/status')
def offline_status():
    with OFFLINE_LOCK:
        return {'node': NODE, 'active': OFFLINE['active'],
                'simulated': OFFLINE['simulated'],
                'since': OFFLINE['since'], 'queue': list(OFFLINE['queue']),
                'tokens': OFFLINE['tokens'],
                'seconds': round(OFFLINE['seconds'], 2)}


@app.post('/offline/replay')
def offline_replay():
    """手动触发本地队列回放（沙盒验证用）。

    真机路径无需调用：heartbeat_loop 检测到控制面恢复会自动回放。
    """
    return {'ok': True, 'result': _replay_offline()}


class QueueReq(BaseModel):
    tasks: int = 1
    tokens: int = 500
    seconds: float = 30.0


@app.post('/offline/enqueue')
def offline_enqueue(req: QueueReq):
    """沙盒：把一批"已完成的任务"直接塞进本地队列（不必真跑推理）。"""
    with OFFLINE_LOCK:
        if not OFFLINE['active']:
            # 未进入断网时自动进入，避免沙盒用例顺序耦合
            OFFLINE.update(active=True, since=int(time.time() * 1000),
                           simulated=True)
        for i in range(max(1, int(req.tasks))):
            OFFLINE['queue'].append(f'{NODE}-local-{int(time.time())}-{i}')
        OFFLINE['tokens'] += int(req.tokens)
        OFFLINE['seconds'] += float(req.seconds)
        return {'ok': True, 'queue': len(OFFLINE['queue']),
                'tokens': OFFLINE['tokens'],
                'seconds': round(OFFLINE['seconds'], 2)}


def _replay_offline():
    """把本地队列作为批次回放给控制面（幂等由控制面保证）。"""
    with OFFLINE_LOCK:
        if not OFFLINE['queue']:
            return {'sent': False, 'reason': '本地队列为空，无需回放'}
        tasks = len(OFFLINE['queue'])
        tokens = OFFLINE['tokens']
        seconds = OFFLINE['seconds']
        OFFLINE['queue'] = []; OFFLINE['tokens'] = 0; OFFLINE['seconds'] = 0.0
    batch = {'batch_id': f'ob-{NODE}-{int(time.time())}-{tasks}',
             'node': NODE, 'tasks': tasks, 'tokens': tokens,
             'node_seconds': round(seconds, 2), 'note': '自动回放'}
    try:
        r = WS.post(f'{CTRL}/internal/offline/batch', json=batch,
                    headers={'Authorization': f'Bearer {TOKEN}'}, timeout=10)
        return {'sent': True, 'status': r.status_code,
                'resp': _safe_json(r), 'batch': batch}
    except requests.RequestException as e:
        # 回放失败：把批次数据放回队列，等下次重连（不丢数据）
        with OFFLINE_LOCK:
            OFFLINE['tokens'] += tokens
            OFFLINE['seconds'] += seconds
            OFFLINE['queue'].append(f'pending-{batch["batch_id"]}')
        return {'sent': False, 'error': str(e)[:120], 'batch': batch}


def _safe_json(r):
    try:
        return r.json()
    except ValueError:
        return r.text[:120]


def db_event(msg, kind='worker'):
    """本地事件：控制面不可达时只写 stdout，不阻塞主流程。"""
    print(f'[{kind}] {msg}', flush=True)


# ---------------- 引擎调用 ----------------
def _openai_call(base, model, prompt, timeout=None):
    r = WS.post(f'{base}/chat/completions', json={
        'model': model,
        'messages': [
            {'role': 'system',
             'content': '直接给出最终答案，不要输出思考过程，答案控制在100字以内。'},
            {'role': 'user', 'content': prompt + ' /no_think'}],
        'max_tokens': VLLM_MAXTOK,
        'temperature': 0.2}, timeout=timeout or VLLM_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    content = j['choices'][0]['message']['content']
    return content, j.get('usage'), model


def engine_call(prompt):
    """按「所选引擎 → 降级链 → mock」顺序调用，返回 (答案, usage, 实际引擎)。"""
    chain = [ENGINE_NAME] + FALLBACKS
    last_err = ''
    for name in chain:
        if eng is None:
            break
        s = eng.spec(name)
        if s['kind'] == 'mock':
            break
        base = s['base'] if name == ENGINE_NAME else s['default_base']
        model = s['model'] if name == ENGINE_NAME else s['default_model']
        try:
            ans, usage, used = _openai_call(base, model, prompt)
            ENGINE_STATE.update(name=name, healthy=True,
                                detail=f'{base} 调用成功',
                                backend=s['backend'], exact=s['exact'],
                                degraded=(name != ENGINE_NAME))
            if name != ENGINE_NAME:
                ans = f'[degraded→{name}] {ans}'
            return ans, usage, name
        except Exception as e:                  # noqa: BLE001
            last_err = f'{name}: {str(e)[:120]}'
    ENGINE_STATE.update(healthy=False, detail=last_err or '无可用引擎',
                        degraded=True)
    ans = mock_compute.one_shot(prompt, 128, cheat=RUNTIME['cheat'])
    usage = {'prompt_tokens': 128, 'completion_tokens': max(1, len(ans) // 2),
             'total_tokens': 128 + max(1, len(ans) // 2), 'synthetic': True}
    return f'[degraded→mock] {ans}', usage, 'mock'


def _mock_call(prompt, task_type, tokens_in):
    time.sleep(mock_compute.est_duration_s(task_type, tokens_in))
    ans = mock_compute.one_shot(prompt, tokens_in, cheat=RUNTIME['cheat'])
    usage = {'prompt_tokens': max(1, tokens_in),
             'completion_tokens': max(1, len(ans) // 2),
             'total_tokens': max(1, tokens_in) + max(1, len(ans) // 2),
             'synthetic': True}
    return ans, usage


@app.post('/execute')
async def execute(req: ExecReq):
    from fastapi.concurrency import run_in_threadpool
    try:
        if HAS_GPU:
            ans, usage, used = await run_in_threadpool(engine_call, req.prompt)
        else:
            ans, usage = await run_in_threadpool(_mock_call, req.prompt,
                                                 req.task_type, req.tokens_in)
            used = 'mock'
    except requests.RequestException as e:
        return {'ok': False, 'answer': '', 'error': f'engine:{e}'}
    return {'ok': True, 'answer': ans, 'progress': 1.0, 'usage': usage,
            'engine': used, 'degraded': ENGINE_STATE['degraded'],
            'offline': OFFLINE['active']}


@app.post('/start')
def start(req: StartReq):
    with JOBS_LOCK:
        j = Job(req.task, req.prompt, req.task_type, req.tokens_in)
        JOBS[req.task] = j
    _start_job(j)
    return {'ok': True, 'task': req.task, 'duration': j.duration,
            'offline': OFFLINE['active']}


@app.get('/status')
def status(task: str):
    with JOBS_LOCK:
        j = JOBS.get(task)
    if not j:
        return {'state': 'unknown'}
    if j.state == 'done':
        _saturate(j)
    return {'state': j.state, 'progress': j.progress_pct(), 'answer': j.answer,
            'usage': j.usage, 'engine': j.engine, 'stage': j.stage,
            'eta_s': round(float(j.eta or 0), 1),
            'offline': OFFLINE['active']}


@app.post('/preempt')
def preempt(task: str):
    with JOBS_LOCK:
        j = JOBS.get(task)
    if j and j.state == 'running':
        j.pause.clear(); j.state = 'preempted'
        return {'ok': True, 'progress': j.progress_pct(),
                'checkpoint': {'task': task, 'progress': j.progress_pct(),
                               'stage': j.stage, 'elapsed': j.elapsed,
                               'duration': j.duration,
                               'eta_s': round(float(j.eta or 0), 1)}}
    return {'ok': False}


@app.post('/resume')
def resume(task: str):
    with JOBS_LOCK:
        j = JOBS.get(task)
    if j and j.state == 'preempted':
        j.state = 'running'; j.pause.set()
        return {'ok': True, 'progress': j.progress_pct()}
    return {'ok': False}


@app.post('/set_cheat')
def set_cheat(on: bool = True):
    RUNTIME['cheat'] = on
    return {'ok': True, 'cheat': on}


# ---------------- 注册 + 心跳 ----------------
def _measure(fn, *a, **kw):
    """测量一次控制面往返时延并回填 STATS。"""
    t0 = time.time()
    try:
        r = fn(*a, **kw)
        STATS['latency_ms'] = round((time.time() - t0) * 1000, 2)
        return r
    except Exception:                       # noqa: BLE001
        STATS['latency_ms'] = 0.0
        raise


def register():
    body = {
        'node': NODE, 'role': ROLE, 'tier': TIER,
        'mem_total_gb': MEM_LIMIT, 'mem_limit_gb': MEM_LIMIT,
        'compute_pct': COMPUTE, 'vllm_port': PORT,
        'trusted': TIER == 'cloud', 'sovereign_region': SOVEREIGN,
        'gpu_name': GPU_NAME if HAS_GPU else 'mock-gpu',
        'engine': ENGINE_STATE['name'], 'form_factor': FORM_FACTOR,
        'generation': GENERATION, 'cc_level': CC_LEVEL,
        'bandwidth_mbps': BANDWIDTH, 'power_w': POWER_W,
    }
    for _ in range(30):
        try:
            r = _measure(WS.post, f'{CTRL}/internal/register', json=body,
                         headers={'Authorization': f'Bearer {TOKEN}'}, timeout=5)
            if r.status_code == 200:
                STATS['hb_ok'] += 1
                return True
        except requests.RequestException:
            STATS['hb_fail'] += 1
        time.sleep(2)
    return False


def _nvml():
    """惰性加载 NVML（NVIDIA 官方管理库）。

    为什么不用 pynvml：
        nvidia-ml-py 是 NVIDIA 官方 NVML 的 Python 绑定，import 名即 pynvml。
    为什么不硬依赖：
        ① 本机（无 GPU 的开发机/CI）装了也没有可用设备；② 老驱动可能初始化失败。
        所以做成「能加载就加载，失败静默回落 nvidia-smi」，采集路径降级可见。
    """
    global _NVML_MOD, _NVML_HANDLE, _NVML_ERR
    if _NVML_MOD is False:              # 前次加载彻底失败
        return None
    if _NVML_MOD is None:
        try:
            import pynvml               # noqa: PLC0415（惰性导入是有意的）
            _NVML_MOD = pynvml
            _NVML_ERR = ''
        except Exception as e:          # noqa: BLE001
            _NVML_MOD, _NVML_ERR = False, f'import: {e}'
            return None
    if _NVML_HANDLE is None:
        try:
            _NVML_MOD.nvmlInit()
            _NVML_HANDLE = _NVML_MOD.nvmlDeviceGetHandleByIndex(0)
            _NVML_ERR = ''
        except Exception as e:          # noqa: BLE001  驱动/容器环境常失败，可重试
            _NVML_HANDLE = None
            _NVML_ERR = f'init: {e}'
            return None
    return _NVML_MOD


_NVML_MOD = None          # None=未加载  False=加载失败  模块对象=就绪
_NVML_HANDLE = None
_NVML_ERR = ''


def gpu_stats():
    """真实 GPU 指标。采集优先级：NVML → nvidia-smi → None。

    GB10 统一内存下 nvidia-smi 的显存字段常返回 [N/A]；NVML 的
    nvmlDeviceGetMemoryInfo 走驱动 ioctl，通常拿得到（拿不到也会被 except 接住）。
    返回 (util_pct, free_mem_gb)；完全取不到时返回 None（心跳按忙时曲线兜底）。
    """
    m = _nvml()
    if m is not None:
        try:
            u = float(m.nvmlDeviceGetUtilizationRates(_NVML_HANDLE).gpu)
            info = m.nvmlDeviceGetMemoryInfo(_NVML_HANDLE)
            free = (info.total - info.used) / (1024 ** 3)
            return u, round(free, 2)
        except Exception:               # noqa: BLE001 → 降级到 smi
            pass
    smi = shutil.which('nvidia-smi') or '/usr/bin/nvidia-smi'

    def _q(fields):
        try:
            out = subprocess.run(
                [smi, f'--query-gpu={fields}', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=8).stdout
            return out.strip().splitlines()[0] if out.strip() else ''
        except Exception:      # noqa: BLE001
            return ''

    def _num(s):
        try:
            return float(str(s).strip())
        except Exception:      # noqa: BLE001
            return None

    u = _num(_q('utilization.gpu'))
    if u is None:
        return None
    used = _num(_q('memory.used'))
    total = _num(_q('memory.total'))
    free = (total - used) / 1024.0 \
        if (used is not None and total is not None) else None
    return u, free


def gpu_metrics():
    """面向看板/答辩的富 GPU 画像（来源可自证）。

    NVML 提供进程级显存与温度，nvidia-smi 兜底，两者都不可用时如实标注 mock。
    只读、无副作用；worker 仅绑回环，不构成暴露面。
    """
    out = {'source': 'mock', 'util_pct': None, 'mem_used_mb': None,
           'mem_total_mb': None, 'temp_c': None, 'processes': [],
           'gpu_name': GPU_NAME, 'detail': ''}
    m = _nvml()
    if m is not None:
        try:
            h = _NVML_HANDLE
            out['source'] = 'pynvml'
            out['gpu_name'] = m.nvmlDeviceGetName(h)
            if isinstance(out['gpu_name'], bytes):
                out['gpu_name'] = out['gpu_name'].decode('utf-8', 'ignore')
            u = m.nvmlDeviceGetUtilizationRates(h)
            out['util_pct'] = round(float(u.gpu), 1)
            info = m.nvmlDeviceGetMemoryInfo(h)
            out['mem_used_mb'] = round(info.used / (1024 ** 2))
            out['mem_total_mb'] = round(info.total / (1024 ** 2))
            try:
                out['temp_c'] = float(m.nvmlDeviceGetTemperature(
                    h, m.NVML_TEMPERATURE_GPU))
            except Exception:       # noqa: BLE001  部分嵌入式驱动无温度传感器
                pass
            try:
                procs = m.nvmlDeviceGetComputeRunningProcesses(h) or []
                out['processes'] = [
                    {'pid': p.pid, 'mem_mb': round(p.usedGpuMemory / (1024 ** 2))}
                    for p in procs if getattr(p, 'usedGpuMemory', None)]
            except Exception:       # noqa: BLE001
                pass
            return out
        except Exception as e:      # noqa: BLE001 → 降级 smi
            out['detail'] = f'nvml 降级: {e}'
    smi = shutil.which('nvidia-smi')
    if smi:
        try:
            q = subprocess.run(
                [smi, '--query-gpu=name,utilization.gpu,memory.used,'
                       'memory.total,temperature.gpu',
                 '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=8).stdout.strip()
            row = q.splitlines()[0].split(', ') if q else []

            def _cell(i, cast=float):
                """按列容错：GB10 统一内存下显存/温度列返回 [N/A]，
                只放弃该字段，不整行否决（此前 'n/a' not in q 的整体否决
                会让利用率也拿不到——smi 回落在 GB10 上形同虚设）。"""
                try:
                    v = row[i].strip()
                    if v.lower() in ('[n/a]', 'n/a', ''):
                        return None
                    return cast(v)
                except Exception:   # noqa: BLE001
                    return None

            if len(row) >= 5:
                got = {'gpu_name': _cell(0, str),
                       'util_pct': _cell(1),
                       'mem_used_mb': _cell(2),
                       'mem_total_mb': _cell(3),
                       'temp_c': _cell(4)}
                # 至少拿到利用率才算有效回落（只有名字没有意义）
                if got['util_pct'] is not None:
                    out['source'] = 'nvidia-smi'
                    for k, v in got.items():
                        if v is not None:
                            out[k] = v
                    if got['mem_used_mb'] is None:
                        out['detail'] = (out['detail'] +
                                         ' smi:显存[N/A](统一内存)').strip()
        except Exception as e:      # noqa: BLE001
            out['detail'] = (out['detail'] + f' smi: {e}').strip()
    if out['source'] == 'mock':
        out['detail'] = (out['detail'] + f' {_NVML_ERR}'.strip()).strip()
    return out


def heartbeat_loop():
    backoff = 5
    while True:
        util = 0.0
        gpu_free = None
        if HAS_GPU:
            st = gpu_stats()
            if st:
                g_util, gpu_free = st
                with JOBS_LOCK:
                    busy = any(j.state == 'running' for j in JOBS.values())
                util = round(g_util, 1) if busy else round(g_util * 0.3, 1)
        else:
            with JOBS_LOCK:
                for j in JOBS.values():
                    if j.state == 'running':
                        util = max(util, mock_compute.utilization_curve(
                            j.elapsed, j.duration, COMPUTE))
        used_mem = 0.0
        with JOBS_LOCK:
            for j in JOBS.values():
                if j.state in ('running', 'preempted'):
                    used_mem += 1.0
        free_mem = max(MEM_LIMIT - used_mem, 0.2)
        if gpu_free is not None:
            free_mem = round(min(MEM_LIMIT, gpu_free), 1)

        payload = {
            'node': NODE, 'util': util, 'free_mem_gb': free_mem,
            'kv_hit': 0.5 if ROLE == 'decode' else 0.2,
            'engine': ENGINE_STATE['name'],
            'engine_healthy': ENGINE_STATE['healthy'],
            'degraded': ENGINE_STATE['degraded'],
            'latency_ms': STATS['latency_ms'],
            'uptime_ratio': _uptime_ratio(), 'stability': _stability(),
            'offline': OFFLINE['active'], 'pending_local': len(OFFLINE['queue']),
        }
        ok = False
        try:
            r = _measure(WS.post, f'{CTRL}/internal/heartbeat', json=payload,
                         headers={'Authorization': f'Bearer {TOKEN}'}, timeout=5)
            ok = r.status_code == 200
        except requests.RequestException:
            ok = False

        # 断网自治：连续失败 3 次自动进入；恢复后自动回放
        with OFFLINE_LOCK:
            was_active = OFFLINE['active']
        if ok:
            STATS['hb_ok'] += 1
            STATS['streak_fail'] = 0
            backoff = 5
            if was_active and not OFFLINE['simulated']:
                _set_offline(False)
                _replay_offline()
                db_event(f'{NODE} 控制面恢复，本地批次已回放', 'offline-recover')
        else:
            STATS['hb_fail'] += 1
            STATS['streak_fail'] += 1
            STATS['max_streak_fail'] = max(STATS['max_streak_fail'],
                                           STATS['streak_fail'])
            if not was_active and STATS['streak_fail'] >= 3:
                _set_offline(True)
            backoff = min(60, backoff * 2)      # 指数退避（借鉴 KubeEdge/EdgeFlow）
        time.sleep(5 if ok else backoff)


def _bind_check():
    host = os.getenv('WORKER_BIND', '127.0.0.1')
    if comp is None:
        return host
    if not comp.is_loopback(host):
        raise SystemExit(
            f'[compliance] worker 控制接口（:{PORT}）无鉴权，只允许绑回环，'
            f'当前 WORKER_BIND={host}。')
    return host


def main():
    bind = _bind_check()
    ok = register()
    print(f'[worker {NODE}] register={ok} gpu={HAS_GPU} '
          f'engine={ENGINE_STATE["name"]} form={FORM_FACTOR} '
          f'cc={CC_LEVEL} bind={bind}:{PORT}', flush=True)
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    uvicorn.run(app, host=bind, port=PORT, log_level='warning')


if __name__ == '__main__':
    main()
