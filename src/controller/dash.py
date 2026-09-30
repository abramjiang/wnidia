# -*- coding: utf-8 -*-
"""WNIDIA 看板（:8888）：只读视图，HTTP Basic 鉴权，数据来自 SQLite。

P2 修正：看板与 API 是**两个进程**，prompt 的隐私 spool 在各自进程内存里、
互不可见。场景编排器因此必须在 **API 进程**里执行（prompt 注册与派发循环
在同一个进程），否则任务一律 prompt_unavailable。所以 /api/demo/* 在这里
只是**代理**到 API（127.0.0.1:API_PORT，Bearer 鉴权），不在本进程执行。
"""
import json as _json
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import HTTPException

from . import config, db, prompt_guard

app = FastAPI(title='WNIDIA Dashboard')
security = HTTPBasic()


def auth(c: HTTPBasicCredentials = Depends(security)):
    if c.username != config.DASH_USER or c.password != config.DASH_PASS:
        raise HTTPException(status_code=401, headers={
            'WWW-Authenticate': 'Basic'})
    return True


@app.get('/healthz')
def healthz():
    return {'ok': True}


@app.get('/api/state', dependencies=[Depends(auth)])
def state():
    """看板数据。

    BUG-V5-04：这里原先直接返回 `t.to_dict()`，**绕过了** `/admin/state` 的
    脱敏闸 —— 而且看板 :8888 正是手册里**有公网映射**的那个端口，
    等于把 prompt 明文摆到了公网。现在与 /admin/state 走同一套脱敏。
    """
    return {
        'nodes': [n.to_dict() for n in db.all_nodes()],
        'tasks': [prompt_guard.redact_task(t.to_dict())
                  for t in db.all_tasks()],
        'events': [e.to_dict() for e in db.recent_events(50)],
        'prompt_policy': prompt_guard.status(),
        # P1：场景编排器实时进度 —— 必须从 API 进程取（RUN 状态在那边）
        'demo': _api_proxy('GET', '/admin/demo/status', timeout=4),
    }


_STATIC = Path(__file__).parent / 'static'


def _page(name: str) -> str:
    """只从 static 目录取文件：名称先做白名单校验，杜绝路径穿越。"""
    allow = {'dashboard.html', 'portal.html', 'jev_sandbox.html'}
    if name not in allow:
        raise HTTPException(status_code=404, detail='page not found')
    p = _STATIC / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail='page not found')
    return p.read_text(encoding='utf-8')


@app.get('/', response_class=HTMLResponse, dependencies=[Depends(auth)])
def index():
    return HTMLResponse(_page('dashboard.html'))


# ---------------- P2：门户页（BP 对齐的场景入口，同一套 Basic 鉴权） ----------------
@app.get('/portal', response_class=HTMLResponse, dependencies=[Depends(auth)])
def portal():
    return HTMLResponse(_page('portal.html'))


@app.get('/jev-sandbox', response_class=HTMLResponse, dependencies=[Depends(auth)])
def jev_sandbox():
    """Jev 决策增强层本地沙盒（纯前端自包含页，可嵌入看板 iframe）。"""
    return HTMLResponse(_page('jev_sandbox.html'))


def _api_proxy(method: str, path: str, timeout: float = 30) -> dict:
    """把 demo 请求转发到 API 进程（编排器必须在派发循环所在进程执行）。

    返回 API 的 JSON；连不上时返回 {'ok': False, 'error': ...}（HTTP 200，
    让门户能读到具体原因而不是只会弹"启动失败"）。
    """
    url = f'http://127.0.0.1:{config.API_PORT}{path}'
    req = urllib.request.Request(
        url, method=method,
        headers={'Authorization': 'Bearer ' + config.API_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return _json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            body = _json.loads(e.read().decode('utf-8'))
        except Exception:      # noqa: BLE001
            body = {}
        return {'ok': False,
                'error': body.get('detail') or body.get('error') or f'HTTP {e.code}'}
    except Exception as e:      # noqa: BLE001
        return {'ok': False, 'error': f'API 不可达：{type(e).__name__}: {e}'[:160]}


@app.get('/api/demo/scenes', dependencies=[Depends(auth)])
def demo_scenes():
    """场景清单（代理到 API 进程；与 /admin/demo/scenes 同源）。"""
    r = _api_proxy('GET', '/admin/demo/scenes')
    return r.get('scenes', r) if isinstance(r, dict) and 'scenes' in r else r


@app.post('/api/demo/run', dependencies=[Depends(auth)])
def demo_run(scene: str = 'edge'):
    """从门户页一键运行场景（代理到 API 进程执行）。

    走看板已有的 HTTP Basic 鉴权（与查看 /api/state 同一权限级别），
    不新增无鉴权的公网端点；错误以 200 + {ok:false,error} 返回，
    门户能显示具体原因（此前只会弹"启动失败"）。
    """
    r = _api_proxy('POST', f'/admin/demo/run?scene={scene}')
    if not r.get('ok'):
        # 不抛 4xx：门户需要拿到 error 文案做友好提示
        return r
    return r


@app.get('/api/demo/status', dependencies=[Depends(auth)])
def demo_status():
    return _api_proxy('GET', '/admin/demo/status')


@app.post('/api/demo/stop', dependencies=[Depends(auth)])
def demo_stop(all: bool = False):
    if all:
        return _api_proxy('POST', '/admin/demo/stop?all=true')
    return _api_proxy('POST', '/admin/demo/stop')


@app.post('/api/demo/pause', dependencies=[Depends(auth)])
def demo_pause():
    return _api_proxy('POST', '/admin/demo/pause')


@app.post('/api/demo/resume', dependencies=[Depends(auth)])
def demo_resume():
    return _api_proxy('POST', '/admin/demo/resume')


@app.post('/api/demo/clear', dependencies=[Depends(auth)])
def demo_clear():
    return _api_proxy('POST', '/admin/demo/clear')


@app.post('/api/bench/best', dependencies=[Depends(auth)])
def bench_best(tasks: int = 6):
    return _api_proxy('POST', f'/admin/bench/best?tasks={tasks}')


@app.get('/api/feedback', dependencies=[Depends(auth)])
def engine_feedback():
    return _api_proxy('GET', '/admin/feedback', timeout=6)


@app.get('/api/jev/report', dependencies=[Depends(auth)])
def jev_report(limit: int = 12):
    return _api_proxy('GET', f'/admin/jev/report?limit={limit}', timeout=8)


@app.post('/api/traffic/start', dependencies=[Depends(auth)])
def traffic_start(concurrency: int = 3, duration: float = 120.0,
                  max_tokens: int = 96):
    return _api_proxy('POST', f'/admin/traffic/start?concurrency={concurrency}'
                              f'&duration={duration}&max_tokens={max_tokens}',
                      timeout=15)


@app.post('/api/traffic/stop', dependencies=[Depends(auth)])
def traffic_stop():
    return _api_proxy('POST', '/admin/traffic/stop', timeout=15)


@app.get('/api/traffic/status', dependencies=[Depends(auth)])
def traffic_status():
    return _api_proxy('GET', '/admin/traffic/status', timeout=6)
