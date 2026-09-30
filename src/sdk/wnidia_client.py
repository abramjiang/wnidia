# -*- coding: utf-8 -*-
"""WNIDIA 轻客户端 SDK（零第三方依赖，仅用标准库）。

BP P13 接入层要求「统一 API 与 SDK」。本客户端覆盖对外稳定的管理面与租户面接口；
完整机器可读契约见运行中的 `GET /openapi.json`。

用法：
    from wnullia_client import WNIDIAClient
    c = WNIDIAClient('http://127.0.0.1:9000', token='...')
    c.health()
    c.chat([{'role': 'user', 'content': '你好'}])
    c.metering()          # 计量与分账
    c.gates()             # GO/NO-GO 门槛
    c.audit_export()      # 审计证据包
    c.tenant_usage('demo')

设计取向（借鉴 openai-python 等客户端的结构，但保持零依赖）：
- 一个 Client 类持有 base_url 与 token；
- 每个端点一个显式方法，返回已解析的 dict；
- 错误统一抛 `WNIDIAError`，并带上状态码与服务端 detail。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

__all__ = ['WNIDIAClient', 'WNIDIAError']

DEFAULT_TIMEOUT = 60


class WNIDIAError(RuntimeError):
    def __init__(self, status, detail, url=''):
        super().__init__(f'HTTP {status}: {detail} ({url})')
        self.status = status
        self.detail = detail
        self.url = url


class WNIDIAClient:
    def __init__(self, base_url='http://127.0.0.1:9000', token='',
                 timeout=DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout

    # ---------------- 内部 ----------------
    def _call(self, method, path, params=None, body=None, auth=True):
        url = self.base_url + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if auth:
            headers['Authorization'] = f'Bearer {self.token}'
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode('utf-8', 'ignore')
                return json.loads(raw) if raw.strip().startswith(('{', '[')) \
                    else raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'ignore')
            try:
                detail = json.loads(raw).get('detail', raw)
            except ValueError:
                detail = raw[:200]
            raise WNIDIAError(e.code, detail, url) from None
        except urllib.error.URLError as e:
            raise WNIDIAError(0, f'连接失败：{e.reason}', url) from None

    def get(self, path, **params):
        return self._call('GET', path, params=params or None)

    def post(self, path, body=None, **params):
        return self._call('POST', path, params=params or None, body=body)

    # ---------------- 基础 ----------------
    def health(self):
        """无需鉴权。"""
        return self._call('GET', '/healthz', auth=False)

    def state(self):
        return self.get('/admin/state')

    def capabilities(self):
        """能力与口径总表（含 status=decision_only/simulated 的对外措辞）。"""
        return self.get('/admin/capabilities')

    # ---------------- 推理 ----------------
    def chat(self, messages, **kw):
        body = {'messages': messages}
        body.update(kw)
        return self.post('/v1/chat/completions', body)

    # ---------------- 管理与治理 ----------------
    def engines(self, probe=True):
        return self.get('/admin/engines', probe=str(bool(probe)).lower())

    def compliance(self):
        return self.get('/admin/compliance')

    def metering(self):
        return self.get('/admin/metering')

    def settlement(self, run=False, **kw):
        if run:
            return self.post('/admin/settlement', None, **kw)
        return self.get('/admin/settlement')

    def gates(self):
        return self.get('/admin/gates')

    def trust(self):
        return self.get('/admin/trust')

    def prove(self, node, cc_level=None):
        p = {'node': node}
        if cc_level:
            p['cc_level'] = cc_level
        return self.post('/admin/trust/prove', None, **p)

    def verify_chain(self):
        return self.get('/admin/audit/verify')

    def audit_export(self, since_ms=0, include_prompts=False):
        return self.post('/admin/audit/export', None, since_ms=since_ms,
                         include_prompts=str(bool(include_prompts)).lower())

    def offline(self):
        return self.get('/admin/offline')

    def qpu(self):
        return self.get('/admin/qpu')

    def qpu_submit(self, circuit, qubits=2, shots=1024, job=None):
        body = {'circuit': circuit, 'qubits': qubits, 'shots': shots}
        if job:
            body['job'] = job
        return self.post('/admin/qpu/submit', body)

    def fleet(self):
        return self.get('/admin/fleet')

    def fleet_register(self, robot, **kw):
        body = {'robot': robot}
        body.update(kw)
        return self.post('/admin/fleet/register', body)

    def fleet_rollout(self, model_name='edge-brain', version='v2',
                      rollout_pct=50, dry_run=False):
        return self.post('/admin/fleet/rollout', {
            'model_name': model_name, 'version': version,
            'rollout_pct': rollout_pct, 'dry_run': dry_run})

    def fleet_dispatch(self, tasks, fleet=None):
        body = {'tasks': tasks}
        if fleet:
            body['fleet'] = fleet
        return self.post('/admin/fleet/dispatch', body)

    def fleet_uplink(self, robot, payload):
        return self.post('/admin/fleet/uplink', {'robot': robot,
                                                 'payload': payload})

    def time_slice(self, window_minutes=60, slice_minutes=15,
                   tenants='demo'):
        return self.get('/admin/time-slice', window_minutes=window_minutes,
                        slice_minutes=slice_minutes, tenants=tenants)

    def subscriptions(self):
        return self.get('/admin/subscriptions')

    # ---------------- 租户面 ----------------
    def tenant_usage(self, tenant, limit=500):
        return self.get(f'/v1/tenant/{urllib.parse.quote(tenant)}/usage',
                        limit=limit)


if __name__ == '__main__':
    import os
    cli = WNIDIAClient(os.getenv('CTRL', 'http://127.0.0.1:9000'),
                       os.getenv('WNIDIA_TOKEN', ''))
    try:
        print(json.dumps(cli.health(), ensure_ascii=False, indent=2))
    except WNIDIAError as e:
        print(f'控制面不可达：{e}')
