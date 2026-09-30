# -*- coding: utf-8 -*-
"""通用 Skill evals 运行器：读取各技能 evals/evals.json 并执行断言。

用法：
    python skills/run_evals.py                 # 跑全部 skill
    python skills/run_evals.py skills/gpu-slicer
    EVAL_PYTHON=/path/to/python python skills/run_evals.py   # 指定解释器

本轮修复（原实现的问题）：
1. `json_instance_count` / `json_max` / `json_contains_codes` / `no_traceback_leak`
   四个断言键虽已写进 evals.json，但运行器**从未实现**——被静默忽略，
   导致"20/20 全绿"名不副实。现已全部实现。
2. 缺 evals/evals.json 的目录会直接抛异常中断整轮评测，现改为跳过并告警。
3. 支持 EVAL_PYTHON 覆盖解释器（避免评测环境里 `python` 与部署环境不一致）。
4. 补 `json_lt` / `json_gt` / `stderr_contains` 三个逃生键，便于写新断言。
"""
import json
import os
import re
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_PYTHON = os.getenv('EVAL_PYTHON', '').strip()


def get_dotted(obj, path):
    cur = obj
    for part in path.split('.'):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None, False
    return cur, True


def _collect_codes(obj, acc=None):
    """从任意 JSON 里刮出所有 code 字段（含 codes 数组）。"""
    if acc is None:
        acc = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'code' and isinstance(v, str):
                acc.add(v)
            elif k == 'codes' and isinstance(v, list):
                acc.update(str(x) for x in v)
            else:
                _collect_codes(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            _collect_codes(x, acc)
    return acc


def check_expect(expect, code, stdout, stderr=''):
    errs = []
    if 'exit_code' in expect and code != expect['exit_code']:
        errs.append(f'exit_code 期望 {expect["exit_code"]} 实际 {code}')
    if expect.get('exit_code_nonzero') and code == 0:
        errs.append('期望非零退出，实际 0')

    json_keys = ('json_assert', 'json_keys', 'json_nonempty', 'json_contains',
                 'json_candidate_nodes', 'json_instance_count', 'json_max',
                 'json_lt', 'json_gt', 'json_contains_codes')

    parsed = None
    if stdout.strip().startswith('{'):
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            pass

    if parsed is not None:
        for k, v in expect.get('json_assert', {}).items():
            got, ok = get_dotted(parsed, k)
            if not ok or got != v:
                errs.append(f'json_assert {k} 期望 {v} 实际 {got}')
        for k in expect.get('json_keys', []):
            _, ok = get_dotted(parsed, k)
            if not ok:
                errs.append(f'缺少键 {k}')
        for k in expect.get('json_nonempty', []):
            got, ok = get_dotted(parsed, k)
            if not ok or not got:
                errs.append(f'键 {k} 应为非空')
        for s in expect.get('json_contains', []):
            blob = json.dumps(parsed, ensure_ascii=False)
            if s not in blob:
                errs.append(f'输出未包含 {s}')
        if 'json_candidate_nodes' in expect:
            nodes = [c.get('node') for c in parsed.get('candidates', [])]
            if nodes != expect['json_candidate_nodes']:
                errs.append(f'候选节点 期望 {expect["json_candidate_nodes"]} '
                            f'实际 {nodes}')
        if 'json_instance_count' in expect:
            ins = parsed.get('instances')
            n = len(ins) if isinstance(ins, list) else None
            if n != expect['json_instance_count']:
                errs.append(f"instances 数量 期望 {expect['json_instance_count']} "
                            f"实际 {n}")
        for k, v in (expect.get('json_max') or {}).items():
            got, ok = get_dotted(parsed, k)
            if not ok or not isinstance(got, (int, float)):
                errs.append(f'json_max {k} 不是数值（实际 {got}）')
            elif got > v:
                errs.append(f'json_max {k} 期望 ≤{v} 实际 {got}')
        for k, v in (expect.get('json_lt') or {}).items():
            got, ok = get_dotted(parsed, k)
            if not ok or not isinstance(got, (int, float)):
                errs.append(f'json_lt {k} 不是数值（实际 {got}）')
            elif not got < v:
                errs.append(f'json_lt {k} 期望 <{v} 实际 {got}')
        for k, v in (expect.get('json_gt') or {}).items():
            got, ok = get_dotted(parsed, k)
            if not ok or not isinstance(got, (int, float)):
                errs.append(f'json_gt {k} 不是数值（实际 {got}）')
            elif not got > v:
                errs.append(f'json_gt {k} 期望 >{v} 实际 {got}')
        if 'json_contains_codes' in expect:
            found = _collect_codes(parsed)
            missing = [c for c in expect['json_contains_codes']
                       if c not in found]
            if missing:
                errs.append(f'缺少诊断码 {missing}（实际 {sorted(found)}）')
    elif any(k in expect for k in json_keys):
        errs.append('期望 JSON 输出，但未解析到 JSON')

    if expect.get('no_traceback_leak') and 'Traceback (most recent call last)' \
            in (stderr or ''):
        errs.append('stderr 泄漏了 Python traceback')
    for s in expect.get('stderr_contains', []):
        if s not in (stderr or ''):
            errs.append(f'stderr 未包含 {s}')
    return errs


def _apply_interp(cmd):
    if EVAL_PYTHON and cmd and cmd[0] in ('python', 'python3'):
        return [EVAL_PYTHON] + list(cmd[1:])
    return list(cmd)


def run_skill(skill_dir):
    evals_path = os.path.join(skill_dir, 'evals', 'evals.json')
    if not os.path.isfile(evals_path):
        print(f'  [SKIP] 缺少 {os.path.relpath(evals_path, ROOT)}')
        return 0, 0
    with open(evals_path, encoding='utf-8') as f:
        data = json.load(f)
    passed, failed = 0, 0
    for case in data.get('evals', []):
        if 'run_in_shell' in case:
            shell_cmd = case['run_in_shell']
            if EVAL_PYTHON:
                shell_cmd = re.sub(r'^\s*python3?\s+', f'{EVAL_PYTHON} ',
                                   shell_cmd)
            p = subprocess.run(shell_cmd, cwd=skill_dir, shell=True,
                               capture_output=True, text=True, timeout=180)
        else:
            p = subprocess.run(_apply_interp(case['run']), cwd=skill_dir,
                               capture_output=True, text=True, timeout=180)
        errs = check_expect(case['expect'], p.returncode, p.stdout, p.stderr)
        if errs:
            failed += 1
            print(f'  [FAIL] {case["id"]}: ' + '; '.join(errs))
        else:
            passed += 1
            print(f'  [PASS] {case["id"]}')
    return passed, failed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    skill_root = os.path.join(ROOT, 'skills')
    if args:
        dirs = [a if os.path.isabs(a) else os.path.join(ROOT, a) for a in args]
    else:
        dirs = [os.path.join(skill_root, d)
                for d in sorted(os.listdir(skill_root))
                if os.path.isdir(os.path.join(skill_root, d))
                and not d.startswith(('.', '_'))
                and d != '__pycache__']
    tp = tf = 0
    for d in sorted(dirs):
        print(f'== {os.path.basename(d.rstrip("/"))} ==')
        p, f = run_skill(d)
        tp += p; tf += f
    print(f'\n合计： 通过 {tp}，失败 {tf}')
    sys.exit(1 if tf else 0)


if __name__ == '__main__':
    main()
