# -*- coding: utf-8 -*-
"""推理数据（prompt）留存策略与对外脱敏。

## 为什么需要这个模块

BP P13 决策③写「控制面/数据面分离、推理数据留本地」，而 v3/v4 的实现把
prompt 整段存进中心 SQLite 的 `tasks.prompt`，`/admin/state` 还会把它全量返回
—— 任何持 Token 的人都能读到**全部历史 prompt**。
这不是「运维承诺」能盖住的，必须落到代码。

## 处理方式（不改调度语义，只改「留不留、给不看」）

1. **默认不落库明文**：`tasks.prompt` 写占位标记 `[REDACTED:...]`，
   另存 `prompt_digest`（sha256 前 16 位）与 `prompt_chars`（长度）。
2. 调度确实需要 prompt（worker `/start`、多数决 `/execute`、JEV 预检），
   因此在**控制面进程内存**里保留一份 spool，任务终态即释放；
   读库时（`db._row_task`）自动从 spool 补回，调用方无感。
3. 所有**对外出口**统一走 `redact_task()` / `redact_text()`，
   默认只给预览 + 摘要。
4. 只有 `WNIDIA_STORE_PROMPT=1`（本地复盘）**且** `WNIDIA_REVEAL_PROMPT=1`
   才可能看到全文 —— 两个开关是「与」关系，避免一个开关就泄露。

## 必须如实告知的代价

控制面进程重启后，**未终态任务**的 prompt 不可恢复（它只在内存里）。
这类任务不会被"拿占位符去跑"，而是在派发前被显式判失败（见
`harness._dispatchable()` 与 `error='prompt_unavailable'`），
避免把 `[REDACTED:...]` 当成真实 prompt 发给引擎、产生假结果。
这是「数据不留中心」的必然成本，写进 `docs/COMPLIANCE.md`。
"""
import hashlib
import threading
from collections import OrderedDict

from . import config

# 落库占位标记：一眼能看出"这里本来有 prompt，但按策略没存"
REDACTED = '[REDACTED:prompt-not-stored]'
REDACTED_PREFIX = '[REDACTED'

_SPOOL = OrderedDict()          # task_id -> prompt（仅进程内存）
_SPOOL_LOCK = threading.RLock()
# 计数器刻意区分两类"补不回"，否则运维会误判（见 rehydrate 注释）：
#   miss_active  = 未终态任务补不回 → 真问题，任务无法派发
#   miss_released= 终态任务补不回 → 设计如此（终态即释放）
STATS = {'stored_plaintext': 0, 'spooled': 0, 'dropped': 0, 'scrubbed': 0,
         'rehydrated': 0, 'miss_active': 0, 'miss_released': 0}


# ---------------------------------------------------------------- 开关
def store_enabled():
    """是否允许把 prompt 明文落库（默认否）。"""
    return bool(config.STORE_PROMPT)


def reveal_enabled():
    """是否允许对外返回 prompt 全文。

    必须**同时**满足：允许落库明文 + 显式允许向外展示。
    两个开关是「与」关系，避免只翻一个开关就泄露。
    """
    return bool(config.REVEAL_PROMPT) and store_enabled()


# ---------------------------------------------------------------- 摘要 / 预览
def digest(text):
    """prompt 摘要（sha256 前 16 位）。不落明文也能证明"是不是同一段输入"。"""
    if not text:
        return ''
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def preview(text, limit=None):
    """脱敏预览：**永远留一截不给**，并报出总长度。

    为什么不能简单 `text[:N]`：短 prompt 往往正是最敏感的（客户名、身份证号、
    口令片段、合同编号）。如果长度 ≤ N 就整段返回，那"预览"等于全文，
    脱敏形同虚设。规则改成：
      - 长文本：给前 N 字符 + 省略计数；
      - 短文本：最多给 75%，其余隐去，并标注"已脱敏 x/y 字符"。
    """
    if not text:
        return ''
    n = max(0, int(limit if limit is not None else config.PROMPT_PREVIEW_CHARS))
    total = len(text)
    if total > n:
        return f'{text[:n]}…(+{total - n} chars)'
    # 短文本：至少隐去 1/4，且至少隐去 1 个字符
    keep = min(total - 1, max(0, int(total * 0.75)))
    if keep <= 0:
        return f'[已脱敏 {total} 字符]'
    return f'{text[:keep]}…（已脱敏 {total - keep}/{total} 字符）'


def is_redacted(value):
    return isinstance(value, str) and value.startswith(REDACTED_PREFIX)


# ---------------------------------------------------------------- 内存 spool
def remember(task_id, prompt):
    """把 prompt 放进进程内存（不落盘），返回是否真的存了。"""
    if not task_id or is_redacted(prompt):
        return False
    with _SPOOL_LOCK:
        _SPOOL[task_id] = prompt
        _SPOOL.move_to_end(task_id)
        STATS['spooled'] += 1
        while len(_SPOOL) > max(1, int(config.PROMPT_SPOOL_MAX)):
            _SPOOL.popitem(last=False)
            STATS['dropped'] += 1
    return True


def recall(task_id):
    with _SPOOL_LOCK:
        return _SPOOL.get(task_id)


def forget(task_id):
    """任务终态时释放内存副本。"""
    with _SPOOL_LOCK:
        return _SPOOL.pop(task_id, None) is not None


def clear():
    with _SPOOL_LOCK:
        n = len(_SPOOL)
        _SPOOL.clear()
    return n


def spool_size():
    with _SPOOL_LOCK:
        return len(_SPOOL)


# ---------------------------------------------------------------- 落库 / 读库
def accept(task_id, prompt):
    """**落库前的唯一闸门**：返回应写入 `tasks.prompt` 的值。

    - 允许落库（`WNIDIA_STORE_PROMPT=1`）→ 原样返回；
    - 不允许 → 把 prompt 记进内存 spool，返回占位标记。

    放在 db 的写入路径上调用，意味着**任何**调用方都绕不过去
    （包括演示注入端点、抢占注入等旁路）。
    """
    if not prompt:
        return ''
    if store_enabled():
        STATS['stored_plaintext'] += 1
        return prompt
    remember(task_id, prompt)
    return REDACTED


TERMINAL_STATES = ('done', 'failed', 'rejected')


def rehydrate(row):
    """读库后补回 prompt（就地修改并返回 row）。

    只有「占位标记 + 内存里还有」才补；补不上时保持占位标记，
    由 caller 判断是否可派发（不可派发就显式失败，绝不拿占位符去跑）。

    补不上时**按任务状态分开计数**（BUG-V5-05）：
      - 未终态任务补不回 → `miss_active`，这是真问题（任务会派发失败）；
      - 终态任务补不回   → `miss_released`，这是设计如此（终态即释放）。
    原先只用一个 `rehydrate_miss` 会把两者混在一起：正常跑完的任务也会
    让这个数一直涨，运维看到会以为"内存机制坏了"。指标不能只会报警、
    不会区分"预期"与"异常"。
    """
    p = row.get('prompt')
    if not is_redacted(p):
        return row
    actual = recall(row.get('task'))
    if actual is None:
        if str(row.get('state') or '') in TERMINAL_STATES:
            STATS['miss_released'] += 1
        else:
            STATS['miss_active'] += 1
    else:
        STATS['rehydrated'] += 1
        row['prompt'] = actual
    return row


# ---------------------------------------------------------------- 对外脱敏
def redact_task(task_dict, reveal=None):
    """把任务字典整理成**可对外输出**的形状。

    输出的 `prompt_state` 只有三种取值，避免"看起来没脱敏"或
    "看起来还在"这类含糊表述：

      - `available`：开关允许且原文就在手里（仅复盘模式 + 非终态任务）；
      - `redacted` ：手里有原文，但按策略只给预览；
      - `released` ：原文已随任务终态释放（或本来就没留存），只能给摘要。

    无论哪种状态，都补齐 `prompt_digest` / `prompt_chars`：
    不泄露内容，却仍能核验"这单用的是哪段输入、有多长"。
    """
    d = dict(task_dict or {})
    raw = d.get('prompt')
    if is_redacted(raw):
        raw = recall(d.get('task'))          # 库里是占位标记，看内存里还有没有
    allow = reveal_enabled() if reveal is None else (bool(reveal)
                                                    and reveal_enabled())
    if not raw:
        d['prompt'] = ''
        d['prompt_state'] = 'released'
    elif allow:
        d['prompt'] = raw
        d['prompt_state'] = 'available'
    else:
        d['prompt'] = preview(raw)
        d['prompt_state'] = 'redacted'
    d['prompt_preview'] = preview(raw) if raw else ''
    d['prompt_redacted'] = d['prompt_state'] != 'available'
    d.setdefault('prompt_digest', digest(raw or ''))
    d.setdefault('prompt_chars', len(raw or ''))
    return d


def redact_text(text, reveal=None):
    """单段文本的对外脱敏（证据包导出等非任务场景用）。"""
    allow = reveal_enabled() if reveal is None else (bool(reveal)
                                                    and reveal_enabled())
    if not text:
        return ''
    return text if allow else preview(text)


# ---------------------------------------------------------------- 自证
def status():
    """给 `/admin/compliance` / 沙盒用的策略视图（不含任何明文）。"""
    return {
        'store_plaintext': store_enabled(),
        'reveal_plaintext': reveal_enabled(),
        'reveal_requested': bool(config.REVEAL_PROMPT),
        'scrub_legacy_on_start': bool(config.SCRUB_LEGACY_PROMPTS),
        'preview_chars': int(config.PROMPT_PREVIEW_CHARS),
        'spool_size': spool_size(),
        'spool_max': int(config.PROMPT_SPOOL_MAX),
        'stats': dict(STATS),
        'placeholder': REDACTED,
        'note': ('默认不落库明文：调度所需 prompt 只在控制面进程内存里，'
                 '任务终态即释放；进程重启后未终态任务的 prompt 不可恢复，'
                 '这类任务会在派发前被显式判失败（不会拿占位符去跑）。'),
        'cost': ('代价：控制面重启后未终态任务无法续跑（prompt 只在内存）。'
                 '这是"数据不留中心"的必然成本，已在 docs/COMPLIANCE.md 登记。'),
    }
