# -*- coding: utf-8 -*-
"""prompt 隐私策略单测（不需要起栈，退出码 0 表示全通过）。

覆盖三件在端到端里不好构造的事：

1. **两个开关是「与」关系**：只开 `WNIDIA_REVEAL_PROMPT` 拿不到明文，
   只开 `WNIDIA_STORE_PROMPT` 也拿不到 —— 必须同时开。
   （用子进程换环境变量跑，避免同进程里配置被缓存。）
2. **派发闸门**：拿着占位标记去派发必须被拒，而不是把
   `[REDACTED:...]` 当真实输入跑出假结果。
3. **落库闸门与历史清理**：写库统一过闸；老库里的明文能被一次性抹掉。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def run_snippet(code, extra_env=None, expect_rc=0):
    """在子进程里跑一段代码（隔离环境变量），返回 (rc, stdout, stderr)。"""
    env = dict(os.environ)
    env.setdefault('WNIDIA_COMPLIANCE_STRICT', '0')
    env.pop('WNIDIA_STORE_PROMPT', None)
    env.pop('WNIDIA_REVEAL_PROMPT', None)
    env.pop('WNIDIA_SCRUB_LEGACY', None)
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=90)
    if p.returncode != expect_rc and p.stderr:
        print('  --- stderr ---')
        print('  ' + p.stderr.strip().replace('\n', '\n  ')[:1200])
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------- 开关矩阵
def t_switches():
    probe = ('import json, sys; sys.path.insert(0, ".")\n'
             'from controller import prompt_guard as pg, config\n'
             'print(json.dumps({"store": pg.store_enabled(),\n'
             '                  "reveal": pg.reveal_enabled(),\n'
             '                  "accepted": pg.accept("t-x", "机密原文")}))\n')

    rc, out, _ = run_snippet(probe)
    d = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
    check('U1 默认：不落库明文、不对外给全文',
          d.get('store') is False and d.get('reveal') is False
          and d.get('accepted', '').startswith('[REDACTED'),
          f"store={d.get('store')} reveal={d.get('reveal')}")

    rc, out, _ = run_snippet(probe, {'WNIDIA_STORE_PROMPT': '1'})
    d = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
    check('U2 只开 STORE_PROMPT：仍不给全文（与关系），但落库是明文',
          d.get('store') is True and d.get('reveal') is False
          and d.get('accepted') == '机密原文',
          f"store={d.get('store')} reveal={d.get('reveal')}")

    rc, out, _ = run_snippet(probe, {'WNIDIA_REVEAL_PROMPT': '1'})
    d = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
    check('U3 只开 REVEAL_PROMPT：拿不到明文（单开关无法泄露）',
          d.get('store') is False and d.get('reveal') is False
          and d.get('accepted', '').startswith('[REDACTED'),
          f"store={d.get('store')} reveal={d.get('reveal')}")

    rc, out, _ = run_snippet(probe, {'WNIDIA_STORE_PROMPT': '1',
                                     'WNIDIA_REVEAL_PROMPT': '1'})
    d = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
    check('U4 两个都开：才允许对外给全文',
          d.get('store') is True and d.get('reveal') is True,
          f"store={d.get('store')} reveal={d.get('reveal')}")


# ---------------------------------------------------------------- 闸门
def t_gates():
    from controller import prompt_guard as pg
    from controller.models import TaskSpec
    from controller import harness

    pg.clear()
    pg.remember('t-run', '真实输入')
    ok, why = harness._dispatchable(TaskSpec(task='t-run', prompt='真实输入'))
    check('U5 真实 prompt 可以派发', ok and why == '', f'why={why!r}')

    ok2, why2 = harness._dispatchable(TaskSpec(task='t-none', prompt=pg.REDACTED))
    check('U6 占位标记必须被拦下（不许拿去跑出假结果）',
          (not ok2) and why2 == 'prompt_unavailable', f'why={why2!r}')

    ok3, why3 = harness._dispatchable(TaskSpec(task='t-empty', prompt=''))
    check('U7 空 prompt 也被拦', (not ok3) and why3 == 'prompt_empty',
          f'why={why3!r}')

    # rehydrate：有内存就补回；没有就保持占位标记（由闸门兜底）
    row = pg.rehydrate({'task': 't-run', 'prompt': pg.REDACTED})
    check('U8 读库时能从内存补回原文', row['prompt'] == '真实输入',
          f"prompt={row['prompt']!r}")
    row2 = pg.rehydrate({'task': 't-gone', 'prompt': pg.REDACTED})
    check('U9 补不回时保持占位标记（不静默变空）',
          pg.is_redacted(row2['prompt']), f"prompt={row2['prompt']!r}")

    # 预览：短文本也必须留一截
    short = '客户A'
    pv = pg.preview(short)
    check('U10 预览对短文本同样脱敏（不会等于全文）',
          pv != short and len(pv) <= len(short) + 20 and '已脱敏' in pv,
          f'preview={pv!r}')


# ---------------------------------------------------------------- 落库与清理
def t_store_and_scrub():
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, 'p.db')
        code = (
            'import json, os, sys, sqlite3\n'
            'sys.path.insert(0, ".")\n'
            'from controller import db, prompt_guard as pg\n'
            'from controller.models import TaskSpec\n'
            'db.upsert_task(TaskSpec(task="t-a", prompt="绝密原文A"))\n'
            'con = sqlite3.connect(os.environ["WNIDIA_DB"])\n'
            'stored = con.execute("SELECT prompt FROM tasks WHERE task=\'t-a\'")\n'
            'stored = stored.fetchone()[0]\n'
            'digest, chars = con.execute(\n'
            '    "SELECT prompt_digest, prompt_chars FROM tasks "\n'
            '    "WHERE task=\'t-a\'").fetchone()\n'
            'con.execute("UPDATE tasks SET prompt=\'遗留明文B\' WHERE task=\'t-a\'")\n'
            'con.commit(); con.close()\n'
            'n = db.scrub_plaintext_prompts()\n'
            # 核对**存储侧**：scrub 是否真的把库里的明文抹掉了。
            # 注意不能用 db.get_task() 读回来判断 —— 读库会从内存 spool 补回
            # 原文，那是活任务的正常路径（U8 已单独覆盖），不是 scrub 没生效。
            'con2 = sqlite3.connect(os.environ["WNIDIA_DB"])\n'
            'raw = con2.execute("SELECT prompt FROM tasks WHERE task=\'t-a\'")'
            '.fetchone()[0]\n'
            'con2.close()\n'
            'print(json.dumps({"stored": stored, "digest": digest,\n'
            '                  "chars": chars, "scrubbed": n,\n'
            '                  "raw_after": raw}))\n')
        rc, out, _ = run_snippet(code, {'WNIDIA_DB': dbp})
        d = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
        check('U11 写库统一过闸：库里是占位符，摘要与长度照常落',
              str(d.get('stored', '')).startswith('[REDACTED')
              and bool(d.get('digest')) and d.get('chars') == 5,
              f"stored={str(d.get('stored'))[:24]} chars={d.get('chars')}")
        check('U12 历史明文可被一次性抹掉（老库升级安全）',
              d.get('scrubbed') == 1
              and str(d.get('raw_after', '')).startswith('[REDACTED'),
              f"scrubbed={d.get('scrubbed')} "
              f"raw_after={str(d.get('raw_after'))[:24]}")


def main():
    t_switches()
    t_gates()
    t_store_and_scrub()
    failed = [r for r in results if not r[1]]
    print('\n' + '=' * 40)
    print(f'通过 {len(results) - len(failed)}/{len(results)}')
    for n, _, d in failed:
        print(f'  - {n} {d}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
