# -*- coding: utf-8 -*-
"""可信层：机密计算层级、远程证明、密钥托管与审计证据包（BP P9 的补足）。

**边界声明（重要）**
本模块在沙盒中以**软件模拟**承载接口与流程：
    - 机密 GPU 环境（CC-L1）需要 H100/B200 的 TEE，本模块不做硬件能力声明；
    - 远程证明（CC-L2）在 `WNIDIA_CC_BACKEND=sim` 下由本地 KMS 签发，
      真机请把 `WNIDIA_CC_BACKEND` 切到 `nvtrust` 并把 `prove()` 接到
      NVIDIA 本地证明服务（NVTrust / nv-attestation-sdk）。**不触碰驱动与系统配置。**
    - 密钥托管（CC-L3）以本地文件 KMS 占位，生产应换 KMS/HSM。

参考实现（借鉴，非拷贝）：
- 证明格式：in-toto 的「主体 + 签名 + 材料」结构与 Sigstore 的
  「证明可独立校验」原则 —— 本项目把 digest/signature/verifier 分开存字段，
  使证明可以在不信任控制面的前提下被第三方复算。
- 审计链：Certificate Transparency（RFC 6962）与 Trillian 的
  「prev_hash + payload_digest → chain_hash」逐条链接思想；
  篡改任意一条都会导致后续 chain_hash 全部不匹配。
"""
import hashlib
import hmac
import json
import os
import secrets
import time

from . import capabilities, db, config

BACKEND = os.getenv('WNIDIA_CC_BACKEND', 'sim')      # sim | nvtrust
ATTEST_TTL = int(os.getenv('WNIDIA_CC_TTL', '900'))  # 证明有效期（秒）


# ---------------------------------------------------------------- 本地 KMS（占位）
def _kms_key_path():
    return config.DATA_DIR / '.kms.key'


def _kms_key():
    """本地 KMS 占位：首次生成 32 字节密钥并落盘（权限 600）。
    生产环境应替换为 KMS/HSM —— 这里只保证接口形状与签名可校验。"""
    p = _kms_key_path()
    try:
        if p.exists():
            return p.read_bytes()
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(key)
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key


def kms_sign(payload: str) -> str:
    key = _kms_key()
    return hmac.new(key, payload.encode('utf-8'), hashlib.sha256).hexdigest()


def kms_verify(payload: str, signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(kms_sign(payload), signature)


# ---------------------------------------------------------------- 证明
def _digest(node, cc_level, nonce, boot_id):
    raw = f'{node}|{cc_level}|{nonce}|{boot_id}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def prove(node_profile, cc_level=None, ttl_s=None):
    """签发一份证明并落库。

    sim 后端：由本地 KMS 对 digest 签名（证明"控制面认可该节点当前画像"）。
    nvtrust 后端：接口占位，未接入前拒绝签发，避免假装有硬件证明。
    """
    cc = cc_level or getattr(node_profile, 'cc_level', 'CC-L0')
    ttl = int(ttl_s if ttl_s is not None else ATTEST_TTL)
    nonce = secrets.token_hex(8)
    boot_id = getattr(node_profile, 'generation', '') or 'gen-unknown'
    digest = _digest(node_profile.node, cc, nonce, boot_id)

    if BACKEND != 'sim':
        # 真机路径未接入：不签发，也不静默通过
        db.add_attestation(node_profile.node, cc, nonce, digest, '', BACKEND,
                           valid=False, ttl_s=ttl,
                           detail='真机证明后端未接入（需 NVTrust/CC 机型）')
        return {'ok': False, 'node': node_profile.node, 'cc_level': cc,
                'backend': BACKEND,
                'reason': '真机证明后端未接入，拒绝签发（不假装具备硬件证明）'}

    sig = kms_sign(digest)
    db.add_attestation(node_profile.node, cc, nonce, digest, sig, 'sim',
                       valid=True, ttl_s=ttl, detail='沙盒软件证明（非硬件 TEE）')
    seal('attestation', f'{node_profile.node}:{cc}', digest)
    db.add_event('cc-prove',
                 f'{node_profile.node} 签发 {cc} 证明（沙盒软件证明，digest='
                 f'{digest[:12]}…，有效期 {ttl}s）', node=node_profile.node)
    return {'ok': True, 'node': node_profile.node, 'cc_level': cc, 'nonce': nonce,
            'digest': digest, 'signature': sig, 'verifier': 'sim',
            'ttl_s': ttl, 'backend': BACKEND}


def verify_attestation(rec=None, node=None, cc_level=None):
    """独立校验一份证明：签名 + 有效期 + 层级匹配。"""
    if rec is None:
        rec = db.latest_attestation(node) if node else None
    if not rec:
        return {'ok': False, 'reason': '无证明记录'}
    if not rec.get('valid'):
        return {'ok': False, 'reason': rec.get('detail') or '证明标记为无效'}
    payload = rec['digest']
    sig_ok = kms_verify(payload, rec.get('signature'))
    age = (time.time() * 1000 - int(rec.get('ts') or 0)) / 1000.0
    fresh = age <= int(rec.get('ttl_s') or 0)
    level_ok = True
    if cc_level:
        level_ok = capabilities.cc_satisfies(rec.get('cc_level', 'CC-L0'), cc_level)
    ok = bool(sig_ok and fresh and level_ok)
    return {'ok': ok, 'node': rec.get('node'), 'cc_level': rec.get('cc_level'),
            'signature_ok': sig_ok, 'fresh': fresh, 'age_s': round(age, 1),
            'ttl_s': rec.get('ttl_s'), 'level_ok': level_ok,
            'backend': rec.get('verifier'),
            'reason': '' if ok else ('签名不匹配' if not sig_ok else
                                     ('证明已过期' if not fresh else '层级不足'))}


def required_cc(task):
    return task.cc_required or capabilities.required_cc(task.secret)


def check_node(node, task):
    """准入用：节点的机密计算能力是否满足任务要求。

    返回 (ok, required_cc, detail)
    - CC-L0 要求：任何节点都可以；
    - 更高要求：节点 cc_level 达标 **且** 有有效证明。
    """
    req = required_cc(task)
    if capabilities.CC_RANK.get(req, 0) <= capabilities.CC_RANK['CC-L0']:
        return True, req, '无需机密计算'
    node_cc = getattr(node, 'cc_level', 'CC-L0')
    if not capabilities.cc_satisfies(node_cc, req):
        return False, req, f'节点能力 {node_cc} < 要求 {req}'
    v = verify_attestation(node=node.node, cc_level=req)
    if not v.get('ok'):
        return False, req, f'证明校验未通过：{v.get("reason")}'
    return True, req, f'能力 {node_cc} 达标且证明有效（age={v.get("age_s")}s）'


def status():
    recs = db.attestations_recent(20)
    valid = sum(1 for r in recs if r.get('valid'))
    return {'backend': BACKEND,
            'backend_note': ('沙盒软件证明；真机需 NVTrust/CC 机型'
                             if BACKEND == 'sim' else '真机证明后端'),
            'ttl_s': ATTEST_TTL,
            'recent_total': len(recs), 'recent_valid': valid,
            'levels': capabilities.CC_LEVELS,
            'secret_to_cc': capabilities.SECRET_TO_MIN_CC,
            'recent': recs[:8]}


# ---------------------------------------------------------------- 审计哈希链
def seal(kind, ref, payload_digest):
    """把一条记录追加进审计链（不可篡改：改动任一条会破坏后续链）。"""
    return db.append_chain(kind, ref, payload_digest)


def verify_chain(limit=5000):
    """重算整条链，检测篡改点。"""
    rows = db.chain_rows(limit)
    prev = ''
    broken = []
    for r in rows:
        raw = f"{prev}|{r['kind']}|{r['ref']}|{r['payload_digest']}|{r['ts']}"
        expect = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        if r['prev_hash'] != prev:
            broken.append({'seq': r['seq'], 'why': 'prev_hash 断链'})
        elif r['chain_hash'] != expect:
            broken.append({'seq': r['seq'], 'why': 'chain_hash 不匹配（内容被改）'})
        prev = r['chain_hash']
    head = rows[-1]['chain_hash'] if rows else ''
    return {'length': len(rows), 'ok': not broken, 'broken': broken,
            'head': head}


# ---------------------------------------------------------------- 证据包
def export_evidence_pack(since_ms=0, include_prompts=False):
    """导出可对外出示的审计证据包。

    v5 起 prompt **默认根本不落库**（见 `controller/prompt_guard.py`），
    因此这里变成两件事：

    1. 默认**不含** prompt 明文，但保留 `prompt_digest` 与 `prompt_chars`
       —— 不泄露内容，却仍能证明"这单用的是哪段输入、有多长"；
    2. `include_prompts=True` **不构成充分条件**：还必须
       `WNIDIA_STORE_PROMPT=1` 且 `WNIDIA_REVEAL_PROMPT=1`
       （`prompt_guard.reveal_enabled()`）才可能拿到全文；
       否则 `prompts_included` 如实回 `False`，不给"看起来给了"的错觉。
    """
    from . import prompt_guard
    want_prompts = bool(include_prompts) and prompt_guard.reveal_enabled()
    tasks = [t for t in db.all_tasks() if t.created_at >= since_ms]
    events = db.events_since(since_ms)
    ledger = db.ledger_between(since_ms, int(time.time() * 1000) + 1)
    atts = db.attestations_recent(500)
    chain = db.chain_rows(5000)

    task_view = []
    for t in tasks:
        d = t.to_dict()
        if want_prompts:
            d['prompt'] = prompt_guard.redact_text(d.get('prompt'),
                                                  reveal=True)
        else:
            d.pop('prompt', None)
            d.pop('answer', None)
        # 无论是否含明文，都给出摘要与长度：可核验、不泄露
        d['prompt_digest'] = prompt_guard.digest(t.prompt or '')
        d['prompt_chars'] = int(t.prompt_chars or len(t.prompt or ''))
        task_view.append(d)

    body = {
        'generated_at': int(time.time() * 1000),
        'window': {'since_ms': since_ms},
        'counts': {'tasks': len(task_view), 'events': len(events),
                   'ledger': len(ledger), 'attestations': len(atts),
                   'chain': len(chain)},
        'tasks': task_view,
        'events': events,
        'ledger': ledger,
        'attestations': atts,
        'chain': chain,
        'prompts_included': want_prompts,
        'prompts_requested': bool(include_prompts),
        'prompt_policy': {
            'store_plaintext': prompt_guard.store_enabled(),
            'reveal_plaintext': prompt_guard.reveal_enabled(),
            'note': ('prompt 默认不落库明文；需 WNIDIA_STORE_PROMPT=1 与 '
                     'WNIDIA_REVEAL_PROMPT=1 同时开启，证据包才可能含全文。'),
        },
    }
    pack_digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode(
            'utf-8')).hexdigest()
    body['pack_digest'] = pack_digest
    body['pack_signature'] = kms_sign(pack_digest)
    body['chain_verification'] = verify_chain()
    from . import metering as _metering          # 局部导入，避免循环依赖
    body['metering_quality'] = _metering.metering_quality(ledger)
    return body


def verify_evidence_pack(pack):
    """校验证据包：签名 + 链条 + 摘要。"""
    if not isinstance(pack, dict):
        return {'ok': False, 'reason': '证据包不是 JSON 对象'}
    digest = pack.get('pack_digest', '')
    sig = pack.get('pack_signature', '')
    body = {k: v for k, v in pack.items()
            if k not in ('pack_digest', 'pack_signature', 'chain_verification',
                         'metering_quality')}
    recomputed = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode(
            'utf-8')).hexdigest()
    return {'ok': bool(digest == recomputed and kms_verify(digest, sig)),
            'digest_match': digest == recomputed,
            'signature_ok': kms_verify(digest, sig),
            'reason': '' if digest == recomputed else 'pack_digest 不匹配（内容被改动）'}
