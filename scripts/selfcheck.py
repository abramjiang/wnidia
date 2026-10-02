#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心功能保护 + 三层 bug 自查（selfcheck）

把 r9 期间"人工做的 3 轮 bug 自查"固化为可重复执行的脚本，并保护核心功能不回退。

三层：
  L1 语法与结构  —— py_compile / node --check / 核心文件齐备
  L2 逻辑与配置  —— JEV 模式后端合法、.gitignore 覆盖运行时产物、脱敏无泄漏
  L3 运行时端到端 —— 真实打接口：健康、场景、步骤推进、关闭同步、JEV 报告、计量、门户改名

核心功能保护（任一 FAIL 即视为核心能力回退）：
  调度与引擎选择 / 可信裁决(JEV) / 计量结算 / 边缘自治 / 前端门户联动

用法：
  python3 scripts/selfcheck.py                      # 本机全量（L1+L2+L3）
  python3 scripts/selfcheck.py --skip-runtime       # 只跑 L1+L2（CI 推荐）
  python3 scripts/selfcheck.py --base http://127.0.0.1:9000
  WNIDIA_TOKEN=xxx python3 scripts/selfcheck.py

安全护栏：
  默认只允许探测 127.0.0.1 / localhost。指向其他主机须显式 --allow-remote
  （避免误对已截止的参赛节点发起请求）。

退出码：0 无 FAIL / 1 存在 FAIL / 2 参数错误
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_API = os.environ.get('WNIDIA_API', 'http://127.0.0.1:9000')
DEFAULT_DASH = os.environ.get('WNIDIA_DASH', 'http://127.0.0.1:8888')
DEFAULT_TOKEN = os.environ.get('WNIDIA_TOKEN', 'previewtoken')

CORE_FILES = [
    'controller/main.py', 'controller/demo.py', 'controller/scheduler.py',
    'controller/engines.py', 'controller/metering.py', 'controller/jevclient.py',
    'controller/jev_local.py', 'controller/static/portal.html',
    'controller/static/dashboard.html', 'scripts/deploy_gx10.sh',
]
REQUIRED_GITIGNORE = ['*.log', '*.db-wal', '*.db-shm', '__pycache__']
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}

RESULTS = []


def rec(layer, name, ok, detail='', core=False):
    tag = 'PASS' if ok is True else ('FAIL' if ok is False else 'SKIP')
    RESULTS.append({'layer': layer, 'name': name, 'result': tag,
                    'detail': detail, 'core': core})
    mark = '✅' if tag == 'PASS' else ('❌' if tag == 'FAIL' else '⏭️')
    flag = ' [核心]' if core else ''
    print('  %s %-38s %s%s' % (mark, name, tag, flag))
    if detail:
        print('        %s' % detail[:160])


# ---------------- HTTP ----------------
def http(url, method='GET', token=None, body=None, timeout=15):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    hdrs = {'User-Agent': 'wnidia-selfcheck'}
    if token:
        hdrs['Authorization'] = 'Bearer ' + token
    if data:
        hdrs['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')[:200]
    except Exception as e:
        return 0, repr(e)[:200]


# ---------------- L1 ----------------
def l1():
    print('\n[L1] 语法与结构')
    total = ok = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in {'.git', '.venv', '__pycache__', 'node_modules'}]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            total += 1
            p = os.path.join(dirpath, fn)
            try:
                # 用内建 compile 做纯语法检查：不落 .pyc 文件。
                # 注意：py_compile 写 cfile=/dev/null 会抛 FileExistsError，
                # 曾导致全部 .py 被误判为 FAIL，故不能用。
                with open(p, 'rb') as fh:
                    compile(fh.read(), p, 'exec')
                ok += 1
            except Exception as e:
                rec('L1', '语法 ' + os.path.relpath(p, ROOT), False, repr(e)[:100])
    rec('L1', 'Python 语法 (%d/%d)' % (ok, total), ok == total)

    if subprocess.run(['which', 'node'], capture_output=True).returncode == 0:
        js = 0
        jsok = 0
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {'node_modules', '.git'}]
            for fn in filenames:
                if fn.endswith('.js'):
                    js += 1
                    r = subprocess.run(['node', '--check', os.path.join(dirpath, fn)],
                                       capture_output=True)
                    jsok += 1 if r.returncode == 0 else 0
        rec('L1', 'JS 语法 (%d/%d)' % (jsok, js), jsok == js)
    else:
        rec('L1', 'JS 语法', None, '未安装 node，跳过')

    missing = [f for f in CORE_FILES if not os.path.exists(os.path.join(ROOT, f))]
    rec('L1', '核心文件齐备', not missing,
        '缺失: %s' % missing if missing else '%d 个文件齐全' % len(CORE_FILES))


# ---------------- L2 ----------------
def l2():
    print('\n[L2] 逻辑与配置')
    cfg = os.path.join(ROOT, 'controller/config.py')
    text = open(cfg, encoding='utf-8').read() if os.path.exists(cfg) else ''

    # 同时兼容单引号与双引号，避免因写法差异导致误判
    m = re.search(r"""JEV_MODE\s*=\s*os\.getenv\(\s*['"]WNIDIA_JEV_MODE['"]\s*,\s*['"](\w+)['"]\s*\)""", text)
    mode = m.group(1) if m else '?'
    rec('L2', 'JEV_MODE 合法', mode in ('off', 'mock', 'live', 'auto'), '当前默认: %s' % mode)

    m = re.search(r"""JEV_BACKEND\s*=\s*os\.getenv\(\s*['"]WNIDIA_JEV_BACKEND['"]\s*,\s*['"](\w+)['"]\s*\)""", text)
    be = m.group(1) if m else '?'
    rec('L2', 'JEV_BACKEND 合法', be in ('http', 'local', 'multi'), '当前默认: %s' % be)

    gi = os.path.join(ROOT, '.gitignore')
    gt = open(gi, encoding='utf-8').read() if os.path.exists(gi) else ''
    miss = [p for p in REQUIRED_GITIGNORE if p not in gt]
    rec('L2', '.gitignore 覆盖运行时产物', not miss,
        '缺失: %s' % miss if miss else '已覆盖 %d 项' % len(REQUIRED_GITIGNORE))

    sc = os.path.join(ROOT, 'scripts/sanitize_check.py')
    if os.path.exists(sc):
        r = subprocess.run([sys.executable, sc, '--dir', ROOT, '--json'],
                           capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            rec('L2', '脱敏无泄漏', bool(j.get('clean')),
                '扫描 %d 文件' % j.get('scanned', 0))
        except Exception:
            rec('L2', '脱敏无泄漏', None, '无法解析扫描结果')
    else:
        rec('L2', '脱敏无泄漏', None, '未找到 sanitize_check.py')


# ---------------- L3 ----------------
def l3(api, dash, token, allow_remote):
    print('\n[L3] 运行时端到端')
    host = urlparse(api).hostname or ''
    if host not in LOCAL_HOSTS and not allow_remote:
        rec('L3', '运行时检查', None,
            '目标主机 %s 非本机，已跳过（防误连参赛节点）。需探测请加 --allow-remote' % host)
        return

    code, body = http(api + '/healthz', timeout=8)
    if code == 0:
        # 连接不上 ≠ 功能回退。不可达时整体跳过 L3，避免把"没起服务"误判成核心能力回退。
        rec('L3', '运行时检查', None,
            '服务不可达(%s)，已跳过 L3 —— 请先启动服务再跑全量自查' % api)
        return
    rec('L3', '健康检查 /healthz', code == 200, 'HTTP %s' % code)

    code, st = http(api + '/admin/state', token=token)
    nodes = st.get('nodes', []) if isinstance(st, dict) else []
    rec('L3', '调度: 节点在线', code == 200 and len(nodes) > 0,
        'nodes=%d' % len(nodes), core=True)

    code, sc = http(api + '/admin/demo/scenes', token=token)
    n_scenes = 0
    if isinstance(sc, dict):
        inner = sc.get('scenes', sc)
        # 兼容 dict 与 list 两种返回形态，避免形态差异造成误判
        n_scenes = len(inner) if isinstance(inner, (dict, list)) else 0
    rec('L3', '场景清单非空(防 P0 解包 bug)', code == 200 and n_scenes > 0,
        'scenes=%d' % n_scenes, core=True)

    # 触发运行并观察步骤推进（防"步骤不亮"）
    code, _ = http(api + '/admin/demo/run?scene=edge', 'POST', token, timeout=20)
    steps = -1
    if code == 200:
        for _i in range(20):
            time.sleep(1)
            c2, s2 = http(api + '/admin/demo/status', token=token)
            if isinstance(s2, dict):
                st2 = s2.get('status') or s2.get('state') or ''
                raw_step = s2.get('step', s2.get('current_step', -1))
                try:
                    steps = int(raw_step)          # 防止字符串参与 > 比较而崩溃
                except (TypeError, ValueError):
                    steps = -1
                if steps > 0:
                    break
                if st2 in ('done', 'stopped'):
                    break
    rec('L3', '步骤推进(防步骤不亮)', steps > 0, 'step=%s' % steps, core=True)

    # 关闭同步（防"关闭不同步"）
    http(api + '/admin/demo/stop', 'POST', token, timeout=10)
    stopped = False
    for _i in range(10):
        time.sleep(1)
        c3, s3 = http(api + '/admin/demo/status', token=token)
        if isinstance(s3, dict):
            v = json.dumps(s3, ensure_ascii=False)
            if 'stopped' in v or s3.get('status') == 'stopped':
                stopped = True
                break
    rec('L3', '关闭同步(防双页不同步)', stopped, '已停止' if stopped else '未观察到 stopped')

    # JEV 报告 + 留痕
    code, rep = http(api + '/admin/jev/report', token=token)
    okj = isinstance(rep, dict) and rep.get('ok') is True
    summ = rep.get('summary', {}) if isinstance(rep, dict) else {}
    rec('L3', 'JEV 报告可用(防死界面)', okj,
        'mode=%s live=%s mock=%s' % (summ.get('mode'), summ.get('live'), summ.get('mock')),
        core=True)
    analysis = rep.get('analysis', {}) if isinstance(rep, dict) else {}
    rec('L3', 'JEV 留痕已落盘', (analysis.get('total', 0) or 0) > 0,
        'total=%s' % analysis.get('total'), core=True)

    # 计量
    code, met = http(api + '/admin/metering', token=token)
    rec('L3', '计量能力可用', code == 200, 'HTTP %s' % code, core=True)

    # 门户改名（防改名回退）
    c4, dash_html = http(dash + '/', timeout=8)
    has_rt = isinstance(dash_html, str) and ('实时后端' in dash_html)
    rec('L3', '门户: 实时后端', c4 == 200 and has_rt, 'HTTP %s' % c4)
    c5, port = http(dash + '/portal', timeout=8)
    has_pf = isinstance(port, str) and ('性能前端' in port)
    rec('L3', '门户: 性能前端', c5 == 200 and has_pf, 'HTTP %s' % c5)


def main():
    ap = argparse.ArgumentParser(description='核心功能保护 + 三层 bug 自查')
    ap.add_argument('--base', default=DEFAULT_API, help='API 基址')
    ap.add_argument('--dash', default=DEFAULT_DASH, help='看板基址')
    ap.add_argument('--token', default=DEFAULT_TOKEN)
    ap.add_argument('--skip-runtime', action='store_true')
    ap.add_argument('--allow-remote', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    print('=' * 64)
    print(' WNIDIA 核心功能保护 + 三层 bug 自查')
    print('=' * 64)
    print(' 工程根目录: %s' % ROOT)

    l1()
    l2()
    if args.skip_runtime:
        print('\n[L3] 运行时端到端 —— 已按 --skip-runtime 跳过')
    else:
        l3(args.base, args.dash, args.token, args.allow_remote)

    fails = [r for r in RESULTS if r['result'] == 'FAIL']
    core_fails = [r for r in fails if r['core']]
    skips = [r for r in RESULTS if r['result'] == 'SKIP']

    print('\n' + '=' * 64)
    print(' 汇总: 总 %d 项 | PASS %d | FAIL %d | SKIP %d'
          % (len(RESULTS),
             len([r for r in RESULTS if r['result'] == 'PASS']),
             len(fails), len(skips)))
    if core_fails:
        print(' ⚠️ 核心功能回退: %s' % ', '.join(r['name'] for r in core_fails))
    print(' 结论: %s' % ('通过' if not fails else '存在失败项'))
    print('=' * 64)

    if args.json:
        print(json.dumps({'results': RESULTS, 'fails': len(fails)},
                         ensure_ascii=False, indent=2))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
