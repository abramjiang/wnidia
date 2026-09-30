# -*- coding: utf-8 -*-
"""自建开源 Jev 决策服务：把 openJev 模型以 Jev 兼容的 /v1/systemone 暴露。

用法：
  pip install -r requirements-jev.txt
  WNIDIA_JEV_MODEL=heman10x/openJev-verdict-2.0 python scripts/serve_jev.py

controller 侧对接（http 后端）：
  export WNIDIA_JEV_MODE=live
  export WNIDIA_JEV_BACKEND=http
  export WNIDIA_JEV_BASE=http://127.0.0.1:8201
  export WNIDIA_JEV_MODEL=heman10x/openJev-verdict-2.0
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from controller import jev_local

MODEL = os.getenv('WNIDIA_JEV_MODEL', 'heman10x/openJev-verdict-2.0')
PORT = int(os.getenv('WNIDIA_JEV_SERVER_PORT', '8201'))

app = FastAPI(title='openJev decision server')


class SystemOneReq(BaseModel):
    model: str = MODEL
    state: dict = {}
    questions: dict = {}


@app.post('/v1/systemone')
def systemone(req: SystemOneReq):
    answers = jev_local.decide(req.model or MODEL, req.state, req.questions)
    return {'answers': answers}


@app.get('/healthz')
def healthz():
    return {'ok': True, 'model': MODEL}


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='info')
