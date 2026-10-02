# 路线 B 改动补丁（6 处）

> 前置：先把 `jev_multi.py` 放到 `controller/` 下。
> 下面所有改动均以「原代码 → 新代码」给出，直接替换即可。

---

## 1. `controller/config.py` — backend 注释（第 100 行附近）

```python
# 原
JEV_BACKEND    = os.getenv('WNIDIA_JEV_BACKEND', 'http')   # http | local

# 新
JEV_BACKEND    = os.getenv('WNIDIA_JEV_BACKEND', 'http')   # http | local | multi
```

---

## 2. `controller/config.py` — 新增 multi 配置（紧跟 `JEV_ADAPTIVE` 之后）

```python
# 普通任务自适应校验：预检低置信时自动补做多数决
JEV_ADAPTIVE   = _bool('WNIDIA_JEV_ADAPTIVE', True)

# ---- 路线B：多模型交叉裁决后端 multi（controller/jev_multi.py）----
# 说明：无需 openJev 权重、无需外部裁决服务，只要有一个 Ollama 端点即可
#       启用**真实**的 live 裁决 —— 用多个模型独立作答并做一致性多数决。
JEV_MULTI_BASE      = os.getenv('WNIDIA_JEV_MULTI_BASE',
                                os.getenv('WNIDIA_JEV_EMBED_BASE',
                                          'http://127.0.0.1:11434'))
# 参与交叉裁决的模型，逗号分隔；留空则自动取 Ollama 本地前两个可用模型
JEV_MULTI_MODELS    = os.getenv('WNIDIA_JEV_MULTI_MODELS', '')
# 归一化相似度阈值：>= 判一致(ok)，否则分歧(div)
JEV_MULTI_THRESHOLD = _float('WNIDIA_JEV_MULTI_THRESHOLD', 0.60)
JEV_MULTI_TIMEOUT   = _float('WNIDIA_JEV_MULTI_TIMEOUT', 300.0)
# CJK 门控开关：False（默认）保留原设计——中文场景不自动信任、不做惩罚；
#               设为 True 则放开，让中文内容也能产出自动裁决结论。
JEV_CJK_TRUST       = _bool('WNIDIA_JEV_CJK_TRUST', False)
```

---

## 3. `controller/jevclient.py` — `effective_backend()` 与 `effective_mode()`

```python
# 原
def effective_backend():
    """返回真实模型后端：'local'（进程内 transformers）或 'http'（Jev 兼容 /v1/systemone）。"""
    b = (getattr(config, 'JEV_BACKEND', 'http') or 'http').lower()
    return 'local' if b == 'local' else 'http'


def effective_mode():
    m = (getattr(config, 'JEV_MODE', 'mock') or 'mock').lower()
    if m == 'auto':
        # local 后端无需外部密钥即可启用；http 后端需显式密钥（自建服务可设占位密钥）
        return 'live' if (getattr(config, 'JEV_KEY', '')
                          or effective_backend() == 'local') else 'mock'
    return m if m in ('off', 'mock', 'live') else 'mock'

# 新
def effective_backend():
    """返回真实模型后端：
    'local' -> 进程内 transformers 加载 openJev 权重（路线A）；
    'multi' -> 多模型交叉裁决，走 Ollama（路线B）；
    'http'  -> Jev 兼容 /v1/systemone 服务。
    """
    b = (getattr(config, 'JEV_BACKEND', 'http') or 'http').lower()
    if b == 'local':
        return 'local'
    if b == 'multi':
        return 'multi'
    return 'http'


def effective_mode():
    m = (getattr(config, 'JEV_MODE', 'mock') or 'mock').lower()
    if m == 'auto':
        # local / multi 后端无需外部密钥即可启用；http 后端需显式密钥（自建服务可设占位密钥）
        return 'live' if (getattr(config, 'JEV_KEY', '')
                          or effective_backend() in ('local', 'multi')) else 'mock'
    return m if m in ('off', 'mock', 'live') else 'mock'
```

---

## 4. `controller/jevclient.py` — `status()` 的 `live_configured`

```python
# 原
        'live_configured': bool(getattr(config, 'JEV_KEY', '')
                                or effective_backend() == 'local'),

# 新
        'live_configured': bool(getattr(config, 'JEV_KEY', '')
                                or effective_backend() in ('local', 'multi')),
```

---

## 5. `controller/jevclient.py` — `_call()` 分发 + 新增 `_multi_call()`

```python
# 原（_call 末尾）
    http  -> 调用 Jev 兼容的 /v1/systemone 端点；
    local -> 进程内 transformers 加载 openJev 权重（缺依赖/权重则抛异常，由 _dispatch 兜底）。
    """
    if effective_backend() == 'local':
        return _local_call(state, questions)
    return _live_call(state, questions)

# 新
    http  -> 调用 Jev 兼容的 /v1/systemone 端点；
    local -> 进程内 transformers 加载 openJev 权重（缺依赖/权重则抛异常，由 _dispatch 兜底）；
    multi -> 多模型交叉裁决，走 Ollama（路线B，缺 Ollama/模型则抛异常，由 _dispatch 兜底）。
    """
    backend = effective_backend()
    if backend == 'local':
        return _local_call(state, questions)
    if backend == 'multi':
        return _multi_call(state, questions)
    return _live_call(state, questions)
```

在 `_local_call` 之后新增：

```python
def _multi_call(state, questions):
    """多模型交叉裁决（路线B）：懒加载，走 Ollama 让多个模型独立作答并做一致性多数决。"""
    from . import jev_multi
    return jev_multi.decide(state, questions)
```

---

## 6. `controller/jevclient.py` — CJK 门控开关（`_live_judge` 末尾）

```python
# 原
    trust = (not cjk) and min_conf >= getattr(config, 'JEV_CONF_AUTO', 0.85)

# 新
    # CJK 门控：默认沿用原设计（中文场景不自动信任、不惩罚）；
    # 设 WNIDIA_JEV_CJK_TRUST=1 可放开，让中文内容也产出自动裁决结论。
    cjk_trust = getattr(config, 'JEV_CJK_TRUST', False)
    trust = (((not cjk) or cjk_trust)
             and min_conf >= getattr(config, 'JEV_CONF_AUTO', 0.85))
```

> 这一处是**中文场景能否真正生效的关键**：不改的话，中文内容即使 `mode=live`，
> `trust` 也恒为 False，`dist` 仍会是全 `na`。

---

## 应用后的启用方式

```bash
export WNIDIA_JEV_MODE=live
export WNIDIA_JEV_BACKEND=multi
export WNIDIA_JEV_MULTI_BASE=http://127.0.0.1:11434
export WNIDIA_JEV_MULTI_MODELS=<模型A>,<模型B>
export WNIDIA_JEV_MULTI_THRESHOLD=0.60
export WNIDIA_JEV_CONF_AUTO=0.60      # multi 的置信度=相似度，0.85 过高
export WNIDIA_JEV_CJK_TRUST=1         # 中文场景放开门控
```
