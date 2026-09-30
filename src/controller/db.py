# -*- coding: utf-8 -*-
"""SQLite 数据访问层（WAL）。API 进程与看板进程共享同一文件。

v4 改造要点：
1. `tasks` 与 `nodes` 均改为**显式列名** + 命名参数（原 tasks 用位置绑定
   `INSERT INTO tasks VALUES(...)`，一旦加字段就会错位——v4 要加 13 个字段）。
2. 迁移统一由 `MIGRATIONS` 驱动：`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`，
   老库（v3）可直接升到 v4，不需要删库丢证据。

参考实现（借鉴，非拷贝）：
- 计量与计费维度：OpenMeter（Apache-2.0，2.1k★）与 FinOps FOCUS 开放口径
  —— 把用量事件与账单维度（tenant / project / service）分开建模。
- 审计哈希链：Certificate Transparency（RFC 6962）与 in-toto 证明格式的
  「前序哈希 + 内容哈希」链接思想。
"""
import hashlib
import json
import sqlite3
import threading
from typing import Optional

from . import config, prompt_guard
from .models import (NodeProfile, TaskSpec, Event, now_ms)

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None


# ---------------------------------------------------------------- 表结构
SCHEMA = '''
CREATE TABLE IF NOT EXISTS nodes(
  node TEXT PRIMARY KEY, role TEXT, tier TEXT, mem_total_gb REAL,
  mem_limit_gb REAL, compute_pct INTEGER, vllm_port INTEGER, trusted INTEGER,
  sovereign_region TEXT, status TEXT, util REAL, free_mem_gb REAL, kv_hit REAL,
  reputation REAL, last_heartbeat INTEGER, registered_at INTEGER,
  gpu_name TEXT, cheat INTEGER, engine TEXT DEFAULT 'mock',
  engine_healthy INTEGER, degraded INTEGER DEFAULT 0,
  bandwidth_mbps REAL DEFAULT 0, uptime_ratio REAL DEFAULT 1,
  stability REAL DEFAULT 1, latency_ms REAL DEFAULT 0, power_w INTEGER DEFAULT 0,
  form_factor TEXT DEFAULT 'unknown', cc_level TEXT DEFAULT 'CC-L0',
  qpu_units INTEGER DEFAULT 0, generation TEXT DEFAULT '',
  offline INTEGER DEFAULT 0, offline_since INTEGER DEFAULT 0,
  pending_local INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS tasks(
  task TEXT PRIMARY KEY, tenant TEXT, prompt TEXT, task_type TEXT, sla TEXT,
  secret TEXT, tokens_in INTEGER, need_mem_gb REAL, cost INTEGER,
  sovereign_region TEXT, verify INTEGER, state TEXT, node TEXT, progress REAL,
  attempts INTEGER, answer TEXT, error TEXT, created_at INTEGER,
  bound_at INTEGER, finished_at INTEGER,
  latency_budget_ms REAL DEFAULT 0, cc_required TEXT DEFAULT '',
  billing_mode TEXT DEFAULT 'token', project TEXT DEFAULT '',
  department TEXT DEFAULT '', prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, metering_estimated INTEGER DEFAULT 0,
  engine TEXT DEFAULT '', exact_required INTEGER DEFAULT 0,
  consistency_ok INTEGER,
  -- v5：prompt 默认不落明文，只落摘要与长度（见 controller/prompt_guard.py）
  prompt_digest TEXT DEFAULT '', prompt_chars INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, message TEXT,
  node TEXT, task TEXT);

CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, tenant TEXT, task TEXT,
  node TEXT, cost INTEGER, note TEXT,
  node_seconds REAL DEFAULT 0, tokens INTEGER DEFAULT 0, seats INTEGER DEFAULT 0,
  billing_mode TEXT DEFAULT 'token', project TEXT DEFAULT '',
  department TEXT DEFAULT '', amount_cny REAL DEFAULT 0,
  estimated INTEGER DEFAULT 0, engine TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS quotas(
  tenant TEXT PRIMARY KEY, quota INTEGER, used INTEGER,
  billing_mode TEXT DEFAULT 'token', project TEXT DEFAULT '',
  department TEXT DEFAULT '', tier TEXT DEFAULT 'P1');

-- 机主贡献结算（BP P7）
CREATE TABLE IF NOT EXISTS settlements(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, period TEXT, node TEXT,
  owner TEXT, effective_units REAL, online_hours REAL,
  gross_cny REAL, owner_share_cny REAL, platform_fee_cny REAL,
  note TEXT DEFAULT '');

-- 机密计算证明记录（BP P9 · CC-L2）
CREATE TABLE IF NOT EXISTS attestations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, node TEXT,
  cc_level TEXT, nonce TEXT, digest TEXT, signature TEXT, verifier TEXT,
  valid INTEGER DEFAULT 1, ttl_s INTEGER DEFAULT 0, detail TEXT DEFAULT '');

-- 审计哈希链（BP P9 · CC-L3）
CREATE TABLE IF NOT EXISTS audit_chain(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, ref TEXT,
  payload_digest TEXT, prev_hash TEXT, chain_hash TEXT);

-- 门槛快照（BP P21）
CREATE TABLE IF NOT EXISTS gate_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, gate TEXT,
  value REAL, target REAL, comparator TEXT, passed INTEGER,
  verdict TEXT, basis TEXT DEFAULT '');

-- 具身机队（BP P18）
CREATE TABLE IF NOT EXISTS robots(
  robot TEXT PRIMARY KEY, fleet TEXT, model_name TEXT, model_version TEXT,
  status TEXT, last_seen INTEGER, tasks_done INTEGER DEFAULT 0,
  battery REAL DEFAULT 100, region TEXT DEFAULT '', note TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS model_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, model_name TEXT,
  version TEXT, stage TEXT, rollout_pct INTEGER, digest TEXT,
  note TEXT DEFAULT '');

-- QPU 资源抽象（BP P18）
CREATE TABLE IF NOT EXISTS qpu_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, job TEXT, backend TEXT,
  qubits INTEGER, shots INTEGER, circuit TEXT, result TEXT,
  status TEXT, note TEXT DEFAULT '');

-- 边缘离线批次（BP P13/P14）
CREATE TABLE IF NOT EXISTS offline_batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, node TEXT, batch_id TEXT,
  tasks INTEGER, tokens INTEGER, node_seconds REAL, replayed INTEGER DEFAULT 0,
  replayed_at INTEGER DEFAULT 0, note TEXT DEFAULT '');

-- Agent 决策留痕（v5.1：LLM 提议 → 内核裁决 → 采纳/回落，全程可审计）
CREATE TABLE IF NOT EXISTS agent_decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, task TEXT,
  mode TEXT DEFAULT 'shadow', source TEXT DEFAULT 'llm',
  proposed_node TEXT DEFAULT '', kernel_node TEXT DEFAULT '',
  agreed INTEGER DEFAULT 0, adopted INTEGER DEFAULT 0,
  veto TEXT DEFAULT '', latency_ms REAL DEFAULT 0);
'''


def conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        with _LOCK:
            if _CONN is None:
                c = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
                c.row_factory = sqlite3.Row
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('PRAGMA synchronous=NORMAL')
                c.execute('PRAGMA busy_timeout=5000')
                _init(c)
                _CONN = c
    return _CONN


# 列级迁移：老库加列（不丢数据）
MIGRATIONS = (
    ('nodes', 'engine', "TEXT DEFAULT 'mock'"),
    ('nodes', 'engine_healthy', 'INTEGER'),
    ('nodes', 'degraded', 'INTEGER DEFAULT 0'),
    ('nodes', 'bandwidth_mbps', 'REAL DEFAULT 0'),
    ('nodes', 'uptime_ratio', 'REAL DEFAULT 1'),
    ('nodes', 'stability', 'REAL DEFAULT 1'),
    ('nodes', 'latency_ms', 'REAL DEFAULT 0'),
    ('nodes', 'power_w', 'INTEGER DEFAULT 0'),
    ('nodes', 'form_factor', "TEXT DEFAULT 'unknown'"),
    ('nodes', 'cc_level', "TEXT DEFAULT 'CC-L0'"),
    ('nodes', 'qpu_units', 'INTEGER DEFAULT 0'),
    ('nodes', 'generation', "TEXT DEFAULT ''"),
    ('nodes', 'offline', 'INTEGER DEFAULT 0'),
    ('nodes', 'offline_since', 'INTEGER DEFAULT 0'),
    ('nodes', 'pending_local', 'INTEGER DEFAULT 0'),
    ('tasks', 'latency_budget_ms', 'REAL DEFAULT 0'),
    ('tasks', 'cc_required', "TEXT DEFAULT ''"),
    ('tasks', 'billing_mode', "TEXT DEFAULT 'token'"),
    ('tasks', 'project', "TEXT DEFAULT ''"),
    ('tasks', 'department', "TEXT DEFAULT ''"),
    ('tasks', 'prompt_tokens', 'INTEGER DEFAULT 0'),
    ('tasks', 'completion_tokens', 'INTEGER DEFAULT 0'),
    ('tasks', 'metering_estimated', 'INTEGER DEFAULT 0'),
    ('tasks', 'engine', "TEXT DEFAULT ''"),
    ('tasks', 'exact_required', 'INTEGER DEFAULT 0'),
    ('tasks', 'consistency_ok', 'INTEGER'),
    ('tasks', 'prompt_digest', "TEXT DEFAULT ''"),
    ('tasks', 'prompt_chars', 'INTEGER DEFAULT 0'),
    ('ledger', 'node_seconds', 'REAL DEFAULT 0'),
    ('ledger', 'tokens', 'INTEGER DEFAULT 0'),
    ('ledger', 'seats', 'INTEGER DEFAULT 0'),
    ('ledger', 'billing_mode', "TEXT DEFAULT 'token'"),
    ('ledger', 'project', "TEXT DEFAULT ''"),
    ('ledger', 'department', "TEXT DEFAULT ''"),
    ('ledger', 'amount_cny', 'REAL DEFAULT 0'),
    ('ledger', 'estimated', 'INTEGER DEFAULT 0'),
    ('ledger', 'engine', "TEXT DEFAULT ''"),
    ('quotas', 'billing_mode', "TEXT DEFAULT 'token'"),
    ('quotas', 'project', "TEXT DEFAULT ''"),
    ('quotas', 'department', "TEXT DEFAULT ''"),
    ('quotas', 'tier', "TEXT DEFAULT 'P1'"),
)

# 表级迁移：老库里不存在的新表（v3 的 executescript 已建旧表，新表需补建）
NEW_TABLES = ('settlements', 'attestations', 'audit_chain', 'gate_snapshots',
              'robots', 'model_versions', 'qpu_jobs', 'offline_batches',
              'agent_decisions')


def _init(c):
    c.executescript(SCHEMA)
    _migrate(c)
    c.commit()


def _migrate(c):
    for table, column, decl in MIGRATIONS:
        try:
            cols = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
        except sqlite3.DatabaseError:
            continue
        if not cols:
            continue
        if column not in cols:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')


def reset_db():
    with _LOCK:
        c = conn()
        for t in ('nodes', 'tasks', 'events', 'ledger', 'quotas', 'settlements',
                  'attestations', 'audit_chain', 'gate_snapshots', 'robots',
                  'model_versions', 'qpu_jobs', 'offline_batches',
                  'agent_decisions'):
            c.execute(f'DELETE FROM {t}')
        c.commit()
    # 库清了，内存里那份 prompt 也必须一起清 —— 否则"重置"之后
    # 旧任务的明文还活在进程内存里，与清库语义不符。
    prompt_guard.clear()


# ---------------------------------------------------------------- 通用工具
def _row_to_dict(r):
    return dict(r) if r is not None else None


def table_columns(table):
    with _LOCK:
        return [r[1] for r in conn().execute(f'PRAGMA table_info({table})')]


def _insert(table, data):
    """按表实际列过滤后插入——防止"对象比表多一列"直接抛错。"""
    cols = [c for c in table_columns(table) if c in data]
    if not cols:
        return None
    sql = (f"INSERT INTO {table}({','.join(cols)}) "
           f"VALUES({','.join(':' + c for c in cols)})")
    with _LOCK:
        c = conn()
        cur = c.execute(sql, {k: data[k] for k in cols})
        c.commit()
        return cur.lastrowid


def query(sql, params=()):
    with _LOCK:
        return [_row_to_dict(r) for r in conn().execute(sql, params).fetchall()]


def execute(sql, params=()):
    with _LOCK:
        c = conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur


# ---------------------------------------------------------------- nodes
NODE_COLS = ('node', 'role', 'tier', 'mem_total_gb', 'mem_limit_gb',
             'compute_pct', 'vllm_port', 'trusted', 'sovereign_region', 'status',
             'util', 'free_mem_gb', 'kv_hit', 'reputation', 'last_heartbeat',
             'registered_at', 'gpu_name', 'cheat', 'engine', 'engine_healthy',
             'degraded', 'bandwidth_mbps', 'uptime_ratio', 'stability',
             'latency_ms', 'power_w', 'form_factor', 'cc_level', 'qpu_units',
             'generation', 'offline', 'offline_since', 'pending_local')


def _norm_node(d):
    d = dict(d)
    for k in ('trusted', 'cheat', 'degraded', 'offline'):
        d[k] = int(bool(d.get(k)))
    eh = d.get('engine_healthy')
    d['engine_healthy'] = None if eh is None else int(bool(eh))
    d['engine'] = d.get('engine') or 'mock'
    d['form_factor'] = d.get('form_factor') or 'unknown'
    d['cc_level'] = d.get('cc_level') or 'CC-L0'
    return d


def upsert_node(p: NodeProfile):
    d = _norm_node(p.to_dict())
    placeholders = ','.join(f':{c}' for c in NODE_COLS)
    updates = ','.join(f'{c}=:{c}' for c in NODE_COLS if c != 'node')
    with _LOCK:
        c = conn()
        c.execute(f'''INSERT INTO nodes({",".join(NODE_COLS)})
          VALUES({placeholders})
          ON CONFLICT(node) DO UPDATE SET {updates}''', d)
        c.commit()


def get_node(node) -> Optional[NodeProfile]:
    with _LOCK:
        r = conn().execute('SELECT * FROM nodes WHERE node=?', (node,)).fetchone()
    return _row_node(r) if r else None


def all_nodes():
    with _LOCK:
        rows = conn().execute('SELECT * FROM nodes ORDER BY node').fetchall()
    return [_row_node(r) for r in rows]


def _row_node(r) -> NodeProfile:
    d = _norm_node(dict(r))
    valid = set(NodeProfile.__dataclass_fields__)
    return NodeProfile(**{k: v for k, v in d.items() if k in valid})


# ---------------------------------------------------------------- tasks
TASK_COLS = ('task', 'tenant', 'prompt', 'task_type', 'sla', 'secret',
             'tokens_in', 'need_mem_gb', 'cost', 'sovereign_region', 'verify',
             'state', 'node', 'progress', 'attempts', 'answer', 'error',
             'created_at', 'bound_at', 'finished_at', 'latency_budget_ms',
             'cc_required', 'billing_mode', 'project', 'department',
             'prompt_tokens', 'completion_tokens', 'metering_estimated',
             'engine', 'exact_required', 'consistency_ok',
             'prompt_digest', 'prompt_chars')


def _norm_task(d):
    d = dict(d)
    for k in ('verify', 'metering_estimated', 'exact_required'):
        d[k] = int(bool(d.get(k)))
    co = d.get('consistency_ok')
    d['consistency_ok'] = None if co is None else int(bool(co))
    return d


def _persist_prompt(t: TaskSpec, d: dict):
    """落库前统一过 prompt 闸门（唯一入口，旁路也绕不过）。

    - `WNIDIA_STORE_PROMPT=1` → 原样写明文；
    - 默认 → 把明文记进进程内存 spool，库里只写占位标记。
    同时补齐 digest / chars —— 即使不存明文，也仍然能证明"是不是同一段输入"、
      以及正确计量 prompt 长度（改写库路径才不会漏掉这两项，所以放在这里）。
    """
    d['prompt'] = prompt_guard.accept(t.task, t.prompt)
    if not d.get('prompt_digest'):
        d['prompt_digest'] = prompt_guard.digest(t.prompt or '')
    if not d.get('prompt_chars'):
        d['prompt_chars'] = len(t.prompt or '')
    return d


def upsert_task(t: TaskSpec):
    d = _persist_prompt(t, _norm_task(t.to_dict()))
    placeholders = ','.join(f':{c}' for c in TASK_COLS)
    updates = ','.join(f'{c}=:{c}' for c in TASK_COLS if c != 'task')
    with _LOCK:
        c = conn()
        c.execute(f'''INSERT INTO tasks({",".join(TASK_COLS)})
          VALUES({placeholders})
          ON CONFLICT(task) DO UPDATE SET {updates}''', d)
        c.commit()


def upsert_tasks(tasks):
    """批量写入（单锁、单事务）。

    为什么需要批量：调度循环跑在**另一个线程**里，若逐个 upsert，一批任务之间
    可能被 `launch_queued()` 插进来，导致本应"同批排队"的任务被拆到两个节拍，
    优先级排队的验证就不再确定性（沙盒 C 用例）。
    """
    if not tasks:
        return 0
    placeholders = ','.join(f':{c}' for c in TASK_COLS)
    updates = ','.join(f'{c}=:{c}' for c in TASK_COLS if c != 'task')
    with _LOCK:
        c = conn()
        for t in tasks:
            c.execute(f'''INSERT INTO tasks({",".join(TASK_COLS)})
              VALUES({placeholders})
              ON CONFLICT(task) DO UPDATE SET {updates}''',
                      _persist_prompt(t, _norm_task(t.to_dict())))
        c.commit()
    return len(tasks)


def get_task(task) -> Optional[TaskSpec]:
    with _LOCK:
        r = conn().execute('SELECT * FROM tasks WHERE task=?', (task,)).fetchone()
    return _row_task(r) if r else None


def tasks_by_state(state):
    with _LOCK:
        rows = conn().execute('SELECT * FROM tasks WHERE state=? ORDER BY created_at',
                              (state,)).fetchall()
    return [_row_task(r) for r in rows]


def queued_tasks_ordered():
    """SLA 优先级排队（BP P10「排队抢占」的补齐）。

    排序：sla-1 → sla-2 → sla-3，同级按创建时间（FIFO）。
    原实现只按 created_at，会让低优任务先于高优任务出队。
    """
    order = "CASE sla WHEN 'sla-1' THEN 0 WHEN 'sla-2' THEN 1 ELSE 2 END"
    with _LOCK:
        rows = conn().execute(
            f'SELECT * FROM tasks WHERE state=? ORDER BY {order}, created_at',
            ('queued',)).fetchall()
    return [_row_task(r) for r in rows]


def all_tasks():
    with _LOCK:
        rows = conn().execute('SELECT * FROM tasks ORDER BY created_at').fetchall()
    return [_row_task(r) for r in rows]


def _row_task(r) -> TaskSpec:
    d = _norm_task(dict(r))
    prompt_guard.rehydrate(d)          # 库里是占位标记时，从内存 spool 补回明文
    valid = set(TaskSpec.__dataclass_fields__)
    return TaskSpec(**{k: v for k, v in d.items() if k in valid})


def scrub_plaintext_prompts(replacement=None) -> int:
    """把库里遗留的 prompt 明文抹成占位标记，返回受影响行数。

    用途：从 v3/v4 老库升级上来时，历史 prompt 是明文写进去的，
    只改新写入路径并不能让旧数据消失。启动时调用一次（可用
    `WNIDIA_SCRUB_LEGACY=0` 关掉），并记录到事件与合规视图。
    """
    repl = replacement if replacement is not None \
        else prompt_guard.REDACTED
    with _LOCK:
        c = conn()
        cur = c.execute(
            'UPDATE tasks SET prompt=? '
            "WHERE prompt IS NOT NULL AND prompt<>'' AND prompt NOT LIKE ?",
            (repl, prompt_guard.REDACTED_PREFIX + '%'))
        n = cur.rowcount
        c.commit()
    if n:
        prompt_guard.STATS['scrubbed'] += n
    return n


# ---------------------------------------------------------------- events
def add_event(kind, message, node='', task=''):
    with _LOCK:
        c = conn()
        c.execute('INSERT INTO events(ts,kind,message,node,task) VALUES(?,?,?,?,?)',
                  (now_ms(), kind, message, node, task))
        c.commit()


def recent_events(limit=80):
    with _LOCK:
        rows = conn().execute(
            'SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [Event(ts=r['ts'], kind=r['kind'], message=r['message'],
                  node=r['node'], task=r['task']) for r in rows]


def events_since(ts, limit=2000):
    with _LOCK:
        rows = conn().execute(
            'SELECT * FROM events WHERE ts>=? ORDER BY id LIMIT ?',
            (ts, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- agent_decisions
def add_agent_decision(task, mode, source, proposed_node, kernel_node,
                       agreed, adopted, veto='', latency_ms=0.0):
    """Agent 提议 → 内核裁决的留痕。只在策略开启时写入（off 档不产生噪声）。"""
    with _LOCK:
        c = conn()
        c.execute('''INSERT INTO agent_decisions(
            ts,task,mode,source,proposed_node,kernel_node,agreed,adopted,veto,
            latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (now_ms(), task, mode, source, proposed_node or '', kernel_node or '',
             int(bool(agreed)), int(bool(adopted)), veto or '', float(latency_ms)))
        c.commit()


def agent_decisions(limit=100):
    with _LOCK:
        rows = conn().execute(
            'SELECT * FROM agent_decisions ORDER BY id DESC LIMIT ?',
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def agent_policy_stats():
    """一致率 / 采纳率 / veto 分布：答辩页与 /admin/agent-policy 的数据源。"""
    with _LOCK:
        rows = conn().execute(
            'SELECT mode,agreed,adopted,veto,source FROM agent_decisions').fetchall()
    n = len(rows)
    if not n:
        return {'proposals': 0, 'agreement_rate': None, 'adopted': 0,
                'adopt_rate': None, 'veto_top': [], 'by_source': {}}
    agreed = sum(r['agreed'] for r in rows)
    adopted = sum(r['adopted'] for r in rows)
    vetoes = {}
    for r in rows:
        if r['veto']:
            vetoes[r['veto']] = vetoes.get(r['veto'], 0) + 1
    by_src = {}
    for r in rows:
        d = by_src.setdefault(r['source'], {'n': 0, 'agreed': 0})
        d['n'] += 1
        d['agreed'] += r['agreed']
    return {
        'proposals': n,
        'agreement_rate': round(agreed / n, 4),
        'adopted': adopted,
        'adopt_rate': round(adopted / n, 4),
        'veto_top': sorted(vetoes.items(), key=lambda kv: -kv[1])[:5],
        'by_source': {k: {'n': v['n'],
                          'agreement_rate': round(v['agreed'] / v['n'], 4)}
                      for k, v in by_src.items()},
    }


def recent_agent_vetoes(limit=10):
    """最近被否的提议理由：回流进下次提议提示词，让 Agent 自我修正。"""
    with _LOCK:
        rows = conn().execute(
            "SELECT veto,COUNT(*) c FROM agent_decisions "
            "WHERE veto<>'' GROUP BY veto ORDER BY c DESC LIMIT ?",
            (limit,)).fetchall()
    return [{'veto': r['veto'], 'count': r['c']} for r in rows]


# ---------------------------------------------------------------- ledger / quota
def ensure_quota(tenant, quota, billing_mode='token', tier='P1'):
    with _LOCK:
        c = conn()
        c.execute('''INSERT INTO quotas(tenant,quota,used,billing_mode,tier)
          VALUES(:tenant,:quota,0,:billing_mode,:tier)
          ON CONFLICT(tenant) DO UPDATE SET quota=:quota''',
                  {'tenant': tenant, 'quota': quota,
                   'billing_mode': billing_mode, 'tier': tier})
        c.commit()


def get_quota(tenant):
    ensure_quota(tenant, config.DEFAULT_QUOTA)
    with _LOCK:
        r = conn().execute('SELECT * FROM quotas WHERE tenant=?', (tenant,)).fetchone()
    return dict(r) if r else {'tenant': tenant, 'quota': config.DEFAULT_QUOTA,
                              'used': 0}


def add_ledger(tenant, task, node, cost, note='', node_seconds=0.0, tokens=0,
               seats=0, billing_mode='token', project='', department='',
               amount_cny=0.0, estimated=False, engine=''):
    with _LOCK:
        c = conn()
        c.execute('''INSERT INTO ledger(ts,tenant,task,node,cost,note,node_seconds,
            tokens,seats,billing_mode,project,department,amount_cny,estimated,engine)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (now_ms(), tenant, task, node, cost, note, float(node_seconds),
                   int(tokens), int(seats), billing_mode, project, department,
                   float(amount_cny), int(bool(estimated)), engine or ''))
        c.execute('UPDATE quotas SET used=used+? WHERE tenant=?', (cost, tenant))
        c.commit()


def refund(tenant, cost):
    with _LOCK:
        c = conn()
        c.execute('UPDATE quotas SET used=MAX(used-?,0) WHERE tenant=?',
                  (cost, tenant))
        c.commit()


def ledger_rows(limit=100, tenant=None):
    with _LOCK:
        if tenant:
            rows = conn().execute(
                'SELECT * FROM ledger WHERE tenant=? ORDER BY id DESC LIMIT ?',
                (tenant, limit)).fetchall()
        else:
            rows = conn().execute(
                'SELECT * FROM ledger ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


def ledger_between(start_ms, end_ms, tenant=None):
    with _LOCK:
        if tenant:
            rows = conn().execute(
                'SELECT * FROM ledger WHERE ts>=? AND ts<? AND tenant=?',
                (start_ms, end_ms, tenant)).fetchall()
        else:
            rows = conn().execute(
                'SELECT * FROM ledger WHERE ts>=? AND ts<?',
                (start_ms, end_ms)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- settlements
def add_settlement(period, node, owner, effective_units, online_hours,
                   gross_cny, owner_share_cny, platform_fee_cny, note=''):
    return _insert('settlements', {
        'ts': now_ms(), 'period': period, 'node': node, 'owner': owner,
        'effective_units': float(effective_units),
        'online_hours': float(online_hours), 'gross_cny': float(gross_cny),
        'owner_share_cny': float(owner_share_cny),
        'platform_fee_cny': float(platform_fee_cny), 'note': note})


def settlement_rows(limit=200, node=None, period=None):
    sql = 'SELECT * FROM settlements'
    where, params = [], []
    if node:
        where.append('node=?'); params.append(node)
    if period:
        where.append('period=?'); params.append(period)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    return query(sql, params)


def clear_settlements(period):
    """删除指定周期的结算记录，返回删除条数（供"先清后写"的幂等结算使用）。

    BUG-V4-18：`settlement.settle()` 的 docstring 承诺"同一 period + node 先清后写"，
    但实现里只有插入没有清理 —— 同一天连点两次结算，账面机主分成翻倍。
    钱的问题不能靠文档承诺，必须有对应的删除动作。
    """
    with _LOCK:
        c = conn()
        cur = c.execute('DELETE FROM settlements WHERE period=?', (period,))
        c.commit()
        return cur.rowcount


# ---------------------------------------------------------------- attestations
def add_attestation(node, cc_level, nonce, digest, signature, verifier,
                    valid=True, ttl_s=0, detail=''):
    return _insert('attestations', {
        'ts': now_ms(), 'node': node, 'cc_level': cc_level, 'nonce': nonce,
        'digest': digest, 'signature': signature, 'verifier': verifier,
        'valid': int(bool(valid)), 'ttl_s': int(ttl_s), 'detail': detail})


def latest_attestation(node):
    rows = query('SELECT * FROM attestations WHERE node=? ORDER BY id DESC LIMIT 1',
                 (node,))
    return rows[0] if rows else None


def attestations_recent(limit=50):
    return query('SELECT * FROM attestations ORDER BY id DESC LIMIT ?', (limit,))


# ---------------------------------------------------------------- audit chain
def chain_head():
    rows = query('SELECT * FROM audit_chain ORDER BY seq DESC LIMIT 1')
    return rows[0] if rows else None


def append_chain(kind, ref, payload_digest):
    """把一条记录**原子地**追加进审计哈希链。

    BUG-V4-02（沙盒 J5 暴露，高危）——原实现有两个缺陷：

      ① `now_ms()` 被调用两次：一次参与哈希计算、一次写入 `ts` 列。
         跨毫秒时两者不同，于是 `chain_hash != sha256(prev|kind|ref|digest|ts)`
         —— 校验时必然报「chain_hash 不匹配（内容被改）」，**审计链会自己
         把自己判成被篡改**。这是"可出示证据"能力的致命缺陷。
      ② `chain_head()` 与 `_insert()` 是两次独立加锁，"读链头 → 落库"之间
         可被另一个线程插入，导致两条记录引用同一个 `prev`（真正的分叉）。

    现在：单次取时间戳 + 全程持同一把可重入锁，读头/算哈希/落库三者原子。
    """
    with _LOCK:
        head = chain_head()                    # 复用同一把 RLock，可重入
        prev = head['chain_hash'] if head else ''
        ts = now_ms()                          # ← 只取一次，哈希与落库共用
        raw = f'{prev}|{kind}|{ref}|{payload_digest}|{ts}'
        chain_hash = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        seq = _insert('audit_chain', {
            'ts': ts, 'kind': kind, 'ref': ref,
            'payload_digest': payload_digest, 'prev_hash': prev,
            'chain_hash': chain_hash})
    return {'seq': seq, 'ts': ts, 'prev_hash': prev, 'chain_hash': chain_hash,
            'kind': kind, 'ref': ref, 'payload_digest': payload_digest}


def chain_rows(limit=500):
    return query('SELECT * FROM audit_chain ORDER BY seq LIMIT ?', (limit,))


# ---------------------------------------------------------------- gate snapshots
def add_gate_snapshot(gate, value, target, comparator, passed, verdict, basis=''):
    return _insert('gate_snapshots', {
        'ts': now_ms(), 'gate': gate, 'value': float(value),
        'target': float(target), 'comparator': comparator,
        'passed': int(bool(passed)), 'verdict': verdict, 'basis': basis})


def gate_snapshot_rows(limit=100):
    return query('SELECT * FROM gate_snapshots ORDER BY id DESC LIMIT ?', (limit,))


def latest_gate_snapshots():
    """每个 gate 取最近一条。"""
    rows = query('SELECT * FROM gate_snapshots ORDER BY id DESC')
    seen, out = set(), []
    for r in rows:
        if r['gate'] in seen:
            continue
        seen.add(r['gate'])
        out.append(r)
    return out


# ---------------------------------------------------------------- robots / models
def upsert_robot(robot, fleet='', model_name='', model_version='', status='online',
                 battery=100.0, region='', note=''):
    with _LOCK:
        c = conn()
        c.execute('''INSERT INTO robots(robot,fleet,model_name,model_version,
            status,last_seen,tasks_done,battery,region,note)
          VALUES(?,?,?,?,?,?,COALESCE((SELECT tasks_done FROM robots WHERE robot=?),0),?,?,?)
          ON CONFLICT(robot) DO UPDATE SET fleet=excluded.fleet,
            model_name=excluded.model_name, model_version=excluded.model_version,
            status=excluded.status, last_seen=excluded.last_seen,
            battery=excluded.battery, region=excluded.region, note=excluded.note''',
                  (robot, fleet, model_name, model_version, status, now_ms(),
                   robot, float(battery), region, note))
        c.commit()


def all_robots():
    return query('SELECT * FROM robots ORDER BY robot')


def add_model_version(model_name, version, stage, rollout_pct, digest='', note=''):
    return _insert('model_versions', {
        'ts': now_ms(), 'model_name': model_name, 'version': version,
        'stage': stage, 'rollout_pct': int(rollout_pct), 'digest': digest,
        'note': note})


def model_version_rows(limit=100):
    return query('SELECT * FROM model_versions ORDER BY id DESC LIMIT ?', (limit,))


def current_model_rollout(model_name):
    rows = query('SELECT * FROM model_versions WHERE model_name=? '
                 'ORDER BY id DESC LIMIT 1', (model_name,))
    return rows[0] if rows else None


# ---------------------------------------------------------------- qpu jobs
def add_qpu_job(job, backend, qubits, shots, circuit, result, status, note=''):
    return _insert('qpu_jobs', {
        'ts': now_ms(), 'job': job, 'backend': backend, 'qubits': int(qubits),
        'shots': int(shots), 'circuit': circuit, 'result': json.dumps(
            result, ensure_ascii=False), 'status': status, 'note': note})


def qpu_job_rows(limit=50):
    return query('SELECT * FROM qpu_jobs ORDER BY id DESC LIMIT ?', (limit,))


# ---------------------------------------------------------------- offline batches
def add_offline_batch(node, batch_id, tasks, tokens, node_seconds,
                      replayed=False, note=''):
    return _insert('offline_batches', {
        'ts': now_ms(), 'node': node, 'batch_id': batch_id, 'tasks': int(tasks),
        'tokens': int(tokens), 'node_seconds': float(node_seconds),
        'replayed': int(bool(replayed)),
        'replayed_at': now_ms() if replayed else 0, 'note': note})


def offline_batch_rows(limit=100, node=None):
    if node:
        return query('SELECT * FROM offline_batches WHERE node=? '
                     'ORDER BY id DESC LIMIT ?', (node, limit))
    return query('SELECT * FROM offline_batches ORDER BY id DESC LIMIT ?', (limit,))


def mark_batch_replayed(batch_id):
    return execute('UPDATE offline_batches SET replayed=1, replayed_at=? '
                   'WHERE batch_id=?', (now_ms(), batch_id))
