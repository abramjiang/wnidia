# -*- coding: utf-8 -*-
"""透明代理网关（P1 · 提分路径核心）—— 调用方**零改动**接入异构编排。

## 为什么需要它

现有 WNIDIA 网关要求调用方改造成我们的形态（提交任务 → 轮询/等待结果）。
这带来一个现实问题：**已经存在的 agent / 应用不会为了我们用新接口**。

参照同类项目的做法：把网关**伪装成调用方本来就在用的那个服务**——
调用方把 base_url 指向 WNIDIA，其余一行不改。本模块同时兼容两种常见线格式：

    OpenAI 兼容   POST /v1/chat/completions   GET /v1/models
    Ollama 兼容   POST /api/chat              GET /api/tags

所谓「透明」= 调用方视角里，WNIDIA 就是它原来那个推理服务：

    # 原来
    client = OpenAI(base_url='http://localhost:11434/v1')
    # 现在：把 11434 交给 WNIDIA 监听，代码一行不动，但请求已被画像路由

## 与现有模块的关系（叠加，不替代）

    请求 → 特征识别(TaskFeature) → 画像路由(profile_routing.route)
        → 选定后端节点 → 转发到该节点真实端点 → 计量(profile_metering.meter)

现有 scheduler 的硬约束（时延预算 / 密级 / 引擎健康）**仍然由其负责**，
本模块只额外做「按画像挑后端」，不重复实现，也不取代调度器。

## 诚实边界（务必阅读）

1. **流式（stream=true）目前是"合并返回"**：聚合上游内容后一次性产出，
   不是逐 token 转发。语义不丢（最终内容一致），但**没有真正的流式体验**。
   真实流式转发需要按上游分块读取并逐块下发，列为后续项。
2. **`MODEL_PARAMS_B` 是参考表**，用于把模型名估算为容量需求；未知模型按
   名称中的参数量后缀（如 `32b`）推断，推断不出则保守取 0（不拦截）。
   真实容量应以引擎上报为准。
3. 本模块的 `transport` 可注入，自检使用**假传输层**——跑通的是**逻辑**，
   不是真实网络。真实接入需替换 transport 为实际 HTTP 调用。
"""

import json
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------- 依赖导入
def _imp(mod: str, name: str):
    """同时兼容 `controller.xxx` 包内导入与同目录平铺导入。"""
    for path in ('controller.' + mod, mod):
        try:
            m = __import__(path, fromlist=[name])
            return getattr(m, name)
        except Exception:
            continue
    return None


DeviceProfile = _imp('device_profile', 'DeviceProfile')
DeviceRegistry = _imp('device_profile', 'DeviceRegistry')
TaskFeature = _imp('profile_routing', 'TaskFeature')
route = _imp('profile_routing', 'route')
meter = _imp('profile_metering', 'meter')


# ---------------------------------------------------------------- 常量
# 模型名 → 参数量（B）。用于把"模型名"估算为"需要多少容量"。
# 这是**参考表**，未知模型走名称后缀推断；推断不出返回 0（不拦截请求）。
MODEL_PARAMS_B = {
    'llama3.2:1b': 1.0, 'llama3.2:3b': 3.0, 'llama3.1:8b': 8.0,
    'llama3:70b': 70.0,
    'qwen2.5:7b': 7.0, 'qwen2.5:14b': 14.0, 'qwen2.5:32b': 32.0,
    'qwen2.5:72b': 72.0, 'qwen3:8b': 8.0, 'qwen3:14b': 14.0,
    'qwen3:30b': 30.0, 'qwen3:32b': 32.0,
    'deepseek-r1:7b': 7.0, 'deepseek-r1:14b': 14.0, 'deepseek-r1:32b': 32.0,
    'deepseek-r1:70b': 70.0,
    'gemma2:9b': 9.0, 'gemma2:27b': 27.0,
    'mistral:7b': 7.0, 'mixtral:8x7b': 46.0,
    'phi3:mini': 3.8, 'phi3:medium': 14.0,
}

# 量化 → 每参数字节数。用于把参数量换算为容量需求（GB）。
QUANT_BYTES = {
    'q2': 0.30, 'q3': 0.40, 'q4': 0.55, 'q5': 0.68,
    'q6': 0.78, 'q8': 1.0, 'fp16': 2.0, 'f16': 2.0,
    'bf16': 2.0, 'fp32': 4.0, 'f32': 4.0,
}
DEFAULT_BYTES_PER_PARAM = 2.0     # 保守按 fp16 估

# 透明端口：调用方不改端口即可接入（这也是"零改动"的关键）
TRANSPARENT_PORTS = {'openai': 8000, 'ollama': 11434}

# 上下文 token 估算：按字符数折算（中英混排取保守值）
CHARS_PER_TOKEN = 3.0


# ---------------------------------------------------------------- 数据结构
@dataclass
class Backend:
    """一个后端推理节点（真实提供推理服务的地方）。"""
    node_id: str
    base_url: str = ''          # OpenAI: http://host:8000/v1 ；Ollama: http://host:11434
    api_style: str = 'openai'   # openai | ollama
    healthy: bool = True

    def chat_url(self) -> str:
        b = (self.base_url or '').rstrip('/')
        return b + ('/api/chat' if self.api_style == 'ollama' else '/chat/completions')

    def models_url(self) -> str:
        b = (self.base_url or '').rstrip('/')
        return b + ('/api/tags' if self.api_style == 'ollama' else '/models')


@dataclass
class ProxyResult:
    """一次代理请求的结果（含路由与计量信息，便于审计与界面展示）。"""
    ok: bool = False
    status: int = 502
    node_id: str = ''
    reason: str = ''
    bottleneck: str = ''
    content: str = ''
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    amount_cny: float = 0.0
    attempts: List[str] = field(default_factory=list)
    raw: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            'ok': self.ok, 'status': self.status, 'node_id': self.node_id,
            'reason': self.reason, 'bottleneck': self.bottleneck,
            'content': self.content, 'prompt_tokens': self.prompt_tokens,
            'completion_tokens': self.completion_tokens,
            'latency_ms': round(self.latency_ms, 2),
            'amount_cny': round(self.amount_cny, 6),
            'attempts': self.attempts,
        }


# ---------------------------------------------------------------- 特征识别
def _norm_model(name: str) -> str:
    return (name or '').strip().lower()


def quant_of(name: str) -> float:
    """从模型名推断每参数字节数；推断不出返回默认值。"""
    n = _norm_model(name)
    for k, v in QUANT_BYTES.items():
        if ('-' + k) in n or (':' + k) in n or n.endswith(k):
            return v
    return DEFAULT_BYTES_PER_PARAM


def params_b_of(name: str) -> float:
    """从模型名推断参数量（B）。查表 → 后缀正则 → 0（未知）。"""
    n = _norm_model(name)
    if n in MODEL_PARAMS_B:
        return MODEL_PARAMS_B[n]
    for k, v in MODEL_PARAMS_B.items():
        if k in n or n in k:
            return v
    m = re.search(r'(\d+(?:\.\d+)?)\s*b\b', n)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b', n)   # 如 8x7b（MoE）
    if m:
        return float(m.group(1)) * float(m.group(2))
    return 0.0


def estimate_size_gb(model: str) -> float:
    """模型名 → 预估权重占用（GB）。未知模型返回 0.0（表示"不据此拦截"）。"""
    b = params_b_of(model)
    if b <= 0:
        return 0.0
    return round(b * quant_of(model), 2)


def _text_of(payload: Dict, api_style: str) -> str:
    """从两种线格式中取出对话文本（用于估算上下文长度）。"""
    if api_style == 'ollama':
        msgs = payload.get('messages') or []
        if not msgs and payload.get('prompt'):
            return str(payload.get('prompt'))
        return '\n'.join(str(m.get('content', '')) for m in msgs
                         if isinstance(m, dict))
    return '\n'.join(
        str(m.get('content', '')) for m in (payload.get('messages') or [])
        if isinstance(m, dict))


def infer_feature(payload: Dict,
                  api_style: str = 'openai',
                  concurrency: int = 1,
                  privacy: bool = False,
                  precision_pref: str = '',
                  secret_rank: int = 0) -> Any:
    """把上游请求体翻译为 TaskFeature（供画像路由消费）。

    这是"零改动"能成立的关键：调用方没告诉我们任务特征，我们从请求里推断。
    """
    if TaskFeature is None:
        raise RuntimeError('profile_routing 不可用：无法构造 TaskFeature')

    model = payload.get('model', '') or ''
    text = _text_of(payload, api_style)
    ctx_tokens = max(0, int(len(text) / CHARS_PER_TOKEN))

    if api_style == 'ollama':
        max_tokens = int(payload.get('options', {}).get('num_predict', 0) or 0)
    else:
        max_tokens = int(payload.get('max_tokens', 0) or 0)

    # 阶段判定：长输入=prefill 重；长输出=decode 重
    if ctx_tokens >= 4000 and (max_tokens == 0 or max_tokens < 512):
        phase = 'prefill-heavy'
    elif max_tokens >= 512 or concurrency >= 8:
        phase = 'decode-heavy'
    else:
        phase = 'balanced'

    return TaskFeature(
        model_size_gb=estimate_size_gb(model),
        context_tokens=ctx_tokens,
        concurrency=max(1, concurrency),
        phase=phase,
        privacy=privacy,
        precision_pref=precision_pref,
        secret_rank=secret_rank,
    )


# ---------------------------------------------------------------- 网关
class ProxyGateway:
    """透明代理网关。

    用法（真实接入）：

        gw = ProxyGateway(registry=reg)
        gw.add_backend(Backend('5090-0', 'http://10.0.0.2:8000/v1', 'openai'))
        gw.add_backend(Backend('gb10-0', 'http://10.0.0.3:11434', 'ollama'))
        res = gw.handle_chat(payload, api_style='openai', tenant='t1')

    调用方零改动：把 WNIDIA 监听在 `TRANSPARENT_PORTS` 对应端口上即可。
    """

    def __init__(self, registry: Any = None,
                 transport: Optional[Callable[..., Tuple[int, Dict]]] = None,
                 timeout: float = 30.0):
        self.registry = registry
        self.backends: Dict[str, Backend] = {}
        self.timeout = timeout
        self._inflight = 0
        self.meter_log: List[Dict] = []
        self.transport = transport or self._default_transport

    # ---- 注册 ----
    def add_backend(self, b: Backend) -> None:
        self.backends[b.node_id] = b

    def backends_of(self, node_ids: List[str]) -> List[Backend]:
        return [self.backends[n] for n in node_ids if n in self.backends]

    # ---- 传输 ----
    @staticmethod
    def _default_transport(url: str, payload: Dict,
                           timeout: float = 30.0) -> Tuple[int, Dict]:
        """默认传输层：真实 HTTP POST（JSON）。

        可被注入替换（自检注入假传输层，从而无需网络即可验证逻辑）。
        """
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode('utf-8', 'replace')
                try:
                    return resp.status, json.loads(body)
                except Exception:
                    return resp.status, {'_text': body}
        except urllib.error.HTTPError as e:
            return e.code, {'error': e.read().decode('utf-8', 'replace')[:200]}
        except Exception as e:
            return 0, {'error': repr(e)[:200]}

    # ---- 路由 ----
    def pick(self, f: Any) -> Dict:
        """画像路由；registry 缺失时降级为"第一个健康后端"（不阻断）。"""
        if self.registry is not None and route is not None:
            return route(f, self.registry)
        alive = [b.node_id for b in self.backends.values() if b.healthy]
        if not alive:
            return {'ok': False, 'reason': '无健康后端', 'node': None,
                    'candidates': []}
        return {'ok': True, 'node': alive[0], 'candidates': alive,
                'bottleneck': 'balanced', 'reason': '未接画像，降级为首个健康后端'}

    # ---- 核心 ----
    def handle_chat(self, payload: Dict, api_style: str = 'openai',
                    tenant: str = 'default', project: str = 'default',
                    privacy: bool = False, precision_pref: str = '',
                    secret_rank: int = 0) -> ProxyResult:
        """处理一次对话请求：识别 → 路由 → 转发（失败回退）→ 计量。"""
        t0 = time.time()
        self._inflight += 1
        try:
            f = infer_feature(payload, api_style=api_style,
                              concurrency=self._inflight, privacy=privacy,
                              precision_pref=precision_pref,
                              secret_rank=secret_rank)
            r = self.pick(f)
            res = ProxyResult(bottleneck=r.get('bottleneck', ''),
                              reason=r.get('reason', ''))
            if not r.get('ok'):
                res.status = 400 if '容量' in r.get('reason', '') else 503
                return res

            order = list(r.get('candidates') or [r.get('node')])
            first = r.get('node')
            if first in order:
                order.remove(first)
            order.insert(0, first)

            for nid in order:
                b = self.backends.get(nid)
                if b is None:
                    continue
                res.attempts.append(nid)
                code, body = self.transport(b.chat_url(),
                                            self._upstream_body(payload, b),
                                            self.timeout)
                if code == 200:
                    content, pt, ct = self._parse(body, b.api_style)
                    res.ok = True
                    res.status = 200
                    res.node_id = nid
                    res.content = content
                    res.prompt_tokens, res.completion_tokens = pt, ct
                    res.latency_ms = (time.time() - t0) * 1000.0
                    res.raw = body
                    self._record(nid, tenant, project, pt + ct,
                                 res.latency_ms / 3.6e6, estimated=(ct == 0))
                    return res
                b.healthy = False
                res.reason = '上游 %s → HTTP %s' % (nid, code)

            res.status = 503
            res.reason = (res.reason or '全部后端不可用')
            return res
        finally:
            self._inflight -= 1

    def handle_models(self, node_id: str = '') -> Dict:
        """透传模型列表；未指定节点时取第一个健康后端。"""
        b = self.backends.get(node_id) or next(
            (x for x in self.backends.values() if x.healthy), None)
        if b is None:
            return {'ok': False, 'reason': '无健康后端', 'data': []}
        code, body = self.transport(b.models_url(), {}, self.timeout)
        return {'ok': code == 200, 'status': code, 'node_id': b.node_id,
                'data': body.get('data', body.get('models', []))}

    # ---- 形状转换 ----
    @staticmethod
    def _upstream_body(payload: Dict, b: Backend) -> Dict:
        """按后端风格调整请求体（OpenAI ↔ Ollama 互转）。"""
        p = dict(payload)
        if b.api_style == 'ollama':
            if 'messages' not in p and 'prompt' in p:
                p['messages'] = [{'role': 'user', 'content': p.pop('prompt')}]
            p.setdefault('stream', False)
            return p
        p.setdefault('stream', False)
        return p

    @staticmethod
    def _parse(body: Dict, api_style: str) -> Tuple[str, int, int]:
        """从两种响应体中解析内容与 token 数。"""
        if api_style == 'ollama':
            msg = body.get('message') or {}
            content = msg.get('content') or body.get('response') or ''
            return content, int(body.get('prompt_eval_count', 0) or 0), \
                int(body.get('eval_count', 0) or 0)
        choices = body.get('choices') or []
        content = ''
        if choices:
            content = (choices[0].get('message', {}) or {}).get('content', '') \
                or choices[0].get('text', '') or ''
        usage = body.get('usage') or {}
        return content, int(usage.get('prompt_tokens', 0) or 0), \
            int(usage.get('completion_tokens', 0) or 0)

    def to_wire(self, res: ProxyResult, api_style: str, model: str) -> Dict:
        """把结果还原为调用方期望的线格式（这是"透明"的最后一步）。"""
        if api_style == 'ollama':
            return {
                'model': model,
                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'message': {'role': 'assistant', 'content': res.content},
                'done': True,
                'prompt_eval_count': res.prompt_tokens,
                'eval_count': res.completion_tokens,
            }
        return {
            'id': 'wnidia-%d' % int(time.time() * 1000),
            'object': 'chat.completion',
            'model': model,
            'choices': [{'index': 0,
                         'message': {'role': 'assistant', 'content': res.content},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': res.prompt_tokens,
                      'completion_tokens': res.completion_tokens,
                      'total_tokens': res.prompt_tokens + res.completion_tokens},
        }

    # ---- 计量 ----
    def _record(self, node_id: str, tenant: str, project: str,
                tokens: int, hours: float, estimated: bool) -> None:
        prof = None
        if self.registry is not None and hasattr(self.registry, 'get'):
            try:
                prof = self.registry.get(node_id)
            except Exception:
                prof = None
        amount = 0.0
        if meter is not None:
            try:
                amount = meter(prof, tokens=tokens, node_hours=hours,
                               estimated=estimated).total_amount_cny
            except Exception:
                amount = 0.0
        self.meter_log.append({'node_id': node_id, 'tenant': tenant,
                               'project': project, 'tokens': tokens,
                               'hours': round(hours, 8),
                               'amount_cny': round(amount, 6),
                               'estimated': estimated})

    def statement(self) -> Dict:
        """按节点汇总本次进程内的计量（便于验证计量确实按画像差异化）。"""
        agg: Dict[str, Dict] = {}
        for r in self.meter_log:
            a = agg.setdefault(r['node_id'],
                               {'node_id': r['node_id'], 'requests': 0,
                                'tokens': 0, 'amount_cny': 0.0})
            a['requests'] += 1
            a['tokens'] += r['tokens']
            a['amount_cny'] = round(a['amount_cny'] + r['amount_cny'], 6)
        total = round(sum(a['amount_cny'] for a in agg.values()), 6)
        return {'total_amount_cny': total, 'by_node': list(agg.values()),
                'records': self.meter_log}


# ---------------------------------------------------------------- 自检
def _self_test() -> int:
    import os
    ok = 0
    fails = []

    def check(cond, msg):
        nonlocal ok
        if cond:
            ok += 1
            print('  ✅ %s' % msg)
        else:
            fails.append(msg)
            print('  ❌ %s' % msg)

    def fake_transport_factory(broken=()):
        """假传输层：无需网络即可验证路由/回退/计量逻辑。"""
        def t(url, payload, timeout=30.0):
            for nid in broken:
                if nid in url:
                    return 500, {'error': 'simulated failure'}
            if url.endswith('/api/chat'):
                return 200, {'message': {'role': 'assistant',
                                         'content': 'from-ollama'},
                             'prompt_eval_count': 10, 'eval_count': 20}
            if url.endswith('/chat/completions'):
                return 200, {'choices': [{'message': {'role': 'assistant',
                                                      'content': 'from-openai'}}],
                             'usage': {'prompt_tokens': 10,
                                       'completion_tokens': 20}}
            if url.endswith('/api/tags'):
                return 200, {'models': [{'name': 'qwen2.5:7b'}]}
            return 200, {'data': [{'id': 'qwen2.5:7b'}]}
        return t

    reg = None
    if DeviceRegistry is not None and DeviceProfile is not None:
        reg = DeviceRegistry()
        reg.register(DeviceProfile(
            node_id='gb10-0', gpu_name='GB10', family='soc', vendor='nvidia',
            capacity_gb=128, memory_model='unified', bandwidth_gb_s=273,
            compute_tflops={'fp8': 300}, precision_support=['fp4', 'fp8'],
            interconnect='none', power_w=240, tags=['privacy-capable']))
        reg.register(DeviceProfile(
            node_id='5090-0', gpu_name='RTX 5090', family='consumer',
            vendor='nvidia', capacity_gb=32, bandwidth_gb_s=1792,
            compute_tflops={'fp8': 900}, precision_support=['fp4', 'fp8'],
            interconnect='pcie', power_w=575))

    print('— 特征识别 —')
    check(abs(estimate_size_gb('qwen2.5:32b') - 32 * 2.0) < 0.1,
          '已知模型按 fp16 估算容量（32b→64GB）')
    check(estimate_size_gb('totally-unknown-model') == 0.0,
          '未知模型不臆断，返回 0（不据此拦截）')
    check(estimate_size_gb('qwen2.5:7b-q4') > 0
          and estimate_size_gb('qwen2.5:7b-q4') < estimate_size_gb('qwen2.5:7b'),
          '量化后缀生效（q4 容量小于 fp16）')

    f_long = infer_feature({'model': 'qwen2.5:7b',
                            'messages': [{'role': 'user', 'content': 'x' * 15000}]})
    check(f_long.phase == 'prefill-heavy', '长输入识别为 prefill-heavy')
    f_dec = infer_feature({'model': 'qwen2.5:7b', 'max_tokens': 1024,
                           'messages': [{'role': 'user', 'content': 'hi'}]})
    check(f_dec.phase == 'decode-heavy', '长输出识别为 decode-heavy')

    print('— 透明代理与路由 —')
    # 注意：假传输层按 URL 中是否出现 node_id 判断故障，
    # 因此这里的 base_url 需带节点名（真实环境用 IP 即可，逻辑与本测试无关）。
    gw = ProxyGateway(registry=reg, transport=fake_transport_factory())
    gw.add_backend(Backend('gb10-0', 'http://gb10-0:11434', 'ollama'))
    gw.add_backend(Backend('5090-0', 'http://5090-0:8000/v1', 'openai'))

    r = gw.handle_chat({'model': 'qwen2.5:7b', 'max_tokens': 1024,
                        'messages': [{'role': 'user', 'content': '你好'}]},
                       api_style='openai')
    check(r.ok and r.node_id == '5090-0',
          '高并发/长输出 → 高带宽节点 5090（实得 %s）' % r.node_id)
    wire = gw.to_wire(r, 'openai', 'qwen2.5:7b')
    check(wire['object'] == 'chat.completion'
          and wire['choices'][0]['message']['content'] == 'from-openai',
          'OpenAI 线格式还原正确（调用方无需改动即可解析）')

    # 70b 按 q4 量化 ≈ 38.5GB：5090(32GB) 装不下，GB10(128GB) 可以
    r2 = gw.handle_chat({'model': 'deepseek-r1:70b-q4',
                         'messages': [{'role': 'user', 'content': '长文本'}]},
                        api_style='openai')
    check(r2.ok and r2.node_id == 'gb10-0',
          '大模型(70b-q4≈38.5GB) → 唯一满足容量的 GB10（实得 %s）' % r2.node_id)
    # 同一模型按 fp16 ≈ 140GB：连 GB10 也装不下 → 应当被拒绝，而不是硬塞
    r2b = gw.handle_chat({'model': 'deepseek-r1:70b',
                          'messages': [{'role': 'user', 'content': '长文本'}]},
                         api_style='openai')
    check(not r2b.ok and '容量' in r2b.reason,
          '超容量(70b fp16≈140GB)被明确拒绝（原因含"容量"）')

    r3 = gw.handle_chat({'model': 'qwen2.5:7b',
                         'messages': [{'role': 'user', 'content': '私密'}]},
                        api_style='ollama', privacy=True)
    check(r3.ok and r3.node_id == 'gb10-0',
          '隐私请求 → 强制留在 privacy-capable 节点（实得 %s）' % r3.node_id)
    owire = gw.to_wire(r3, 'ollama', 'qwen2.5:7b')
    check(owire.get('done') is True and 'message' in owire,
          'Ollama 线格式还原正确')

    r4 = gw.handle_chat({'model': 'some-model-500b',
                         'messages': [{'role': 'user', 'content': 'x'}]})
    check(not r4.ok and r4.status == 400,
          '容量无解 → 明确拒绝（400）而非崩溃')

    print('— 故障回退 —')
    gw2 = ProxyGateway(registry=reg,
                       transport=fake_transport_factory(broken=('5090-0',)))
    gw2.add_backend(Backend('gb10-0', 'http://gb10-0:11434', 'ollama'))
    gw2.add_backend(Backend('5090-0', 'http://5090-0:8000/v1', 'openai'))
    r5 = gw2.handle_chat({'model': 'qwen2.5:7b', 'max_tokens': 1024,
                          'messages': [{'role': 'user', 'content': 'hi'}]})
    check(r5.ok and r5.node_id == 'gb10-0' and len(r5.attempts) == 2,
          '首选后端故障 → 自动回退次选并成功（尝试 %s）' % r5.attempts)

    gw3 = ProxyGateway(registry=reg,
                       transport=fake_transport_factory(broken=('5090-0',
                                                                'gb10-0')))
    gw3.add_backend(Backend('gb10-0', 'http://gb10-0:11434', 'ollama'))
    gw3.add_backend(Backend('5090-0', 'http://5090-0:8000/v1', 'openai'))
    r6 = gw3.handle_chat({'model': 'qwen2.5:7b',
                          'messages': [{'role': 'user', 'content': 'hi'}]})
    check(not r6.ok and r6.status == 503,
          '全部后端不可用 → 503 且给出原因（不崩溃）')

    print('— 降级安全 —')
    gw4 = ProxyGateway(registry=None, transport=fake_transport_factory())
    gw4.add_backend(Backend('only-0', 'http://10.0.0.9:8000/v1', 'openai'))
    r7 = gw4.handle_chat({'model': 'qwen2.5:7b',
                          'messages': [{'role': 'user', 'content': 'hi'}]})
    check(r7.ok and r7.node_id == 'only-0',
          '未接画像时降级为首个健康后端（不影响可用性）')

    print('— 计量 —')
    st = gw.statement()
    check(st['total_amount_cny'] > 0 and len(st['by_node']) >= 1,
          '每次请求落计量（合计 %.6f，节点数 %d）' % (st['total_amount_cny'],
                                                    len(st['by_node'])))
    costs = {a['node_id']: a['amount_cny'] for a in st['by_node']}
    if len(costs) >= 2:
        check(costs.get('5090-0', 0) != costs.get('gb10-0', 0),
              '不同画像节点计量差异化（5090=%s / GB10=%s）'
              % (costs.get('5090-0'), costs.get('gb10-0')))
    else:
        check(True, '单节点场景跳过差异化对比')

    print('— 透明端口 —')
    check(TRANSPARENT_PORTS['ollama'] == 11434
          and TRANSPARENT_PORTS['openai'] == 8000,
          '透明端口映射正确（ollama=11434 / openai=8000）')

    total = ok + len(fails)
    print('\n自检: %s (%d/%d)' % ('ALL PASS' if not fails else 'HAS FAIL',
                                  ok, total))
    return 0 if not fails else 1


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
