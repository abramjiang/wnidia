#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脱敏校验器（可复用安全自查工具）

用途：提交/推送前扫描代码与文档，确保没有泄漏真实 IP、端口、用户名、密码、密钥、邮箱。

设计原则：**不在工具内硬编码任何真实敏感串**（否则工具自身就成了泄漏源）。
采用「模式识别」而非「字面量清单」：
  - 公网 IPv4：按模式匹配，并排除回环 / 私有网段 / 文档示例段
  - 密钥：ghp_/AKIA/sk-/私钥头 等通用模式
  - 邮箱：排除常见占位符
环境特有的字面量（SSH 用户名、非标准端口、主机名等）通过外部规则文件传入，
该文件**不应提交**（建议加入 .gitignore）。

用法：
  python3 scripts/sanitize_check.py --dir .
  python3 scripts/sanitize_check.py --dir . --fix          # 自动替换为占位符
  python3 scripts/sanitize_check.py --dir . --rules rules.json
  GH_TOKEN=xxx python3 scripts/sanitize_check.py --repo owner/name
  python3 scripts/sanitize_check.py --dir . --json         # CI 友好
  python3 scripts/sanitize_check.py --dir . --strict       # 内网 IP 也判失败

退出码：0 干净 / 1 发现泄漏 / 2 参数或运行错误
"""

import argparse
import base64
import ipaddress
import json
import os
import re
import sys
import urllib.request
import urllib.parse

IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')

# 正则类（严重，直接判失败）
REGEX_RULES = {
    'GitHub令牌':   r'gh[pousr]_[A-Za-z0-9]{20,}',
    'AWS密钥':      r'AKIA[0-9A-Z]{16}',
    'OpenAI密钥':   r'sk-[A-Za-z0-9]{20,}',
    '私钥':        r'-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----',
}
# 警告类：只提示不判失败（变量赋值在测试/字段定义中很常见，误报率高）
WARN_RULES = {
    '疑似口令赋值': r'(?:PASSWORD|PASSWD|SECRET|TOKEN)\s*=\s*["\']?[A-Za-z0-9_\-]{8,}',
}
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PLACEHOLDER_EMAIL = {'a@b.com', 'alice@example.com', 'bob@example.com',
                     'user@example.com', 'test@example.com', 'foo@bar.com'}

TEXT_EXT = ('.py', '.md', '.sh', '.yml', '.yaml', '.json', '.txt', '.html',
            '.cfg', '.ini', '.toml', '.csv', '.js', '.ts', '.env',
            '.dockerignore', '')
SKIP_DIRS = {'.git', '.venv', '__pycache__', 'node_modules', '.mypy_cache',
             '.pytest_cache', '.idea', '.vscode'}
SKIP_NAMES = {'.DS_Store'}


def is_private_or_doc(ip_str):
    """回环 / 私有 / 文档示例 / 未指定地址，视为无需告警。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return True
    if ip.is_private:
        return True
    # 文档保留段
    if str(ip).startswith('192.0.2.') or str(ip).startswith('198.51.100.') \
            or str(ip).startswith('203.0.113.'):
        return True
    return False


def scan_text(text, rules):
    """返回 (严重命中 dict, 内网IP list, 邮箱 set, 警告 dict)。"""
    severe, private, warn = {}, [], {}
    for name, pat in REGEX_RULES.items():
        m = re.findall(pat, text)
        if m:
            severe[name] = len(m)
    for name, pat in WARN_RULES.items():
        m = re.findall(pat, text)
        if m:
            warn[name] = len(m)
    for ip in set(IPV4_RE.findall(text)):
        if is_private_or_doc(ip):
            # 私有段仅在 --strict 下告警，这里先收集
            if not (ip.startswith('127.') or ip == '0.0.0.0'):
                private.append(ip)
        else:
            severe.setdefault('公网IP', []).append(ip)
    # 外部字面量规则
    for name, (pats, _ph) in rules.items():
        n = sum(text.count(p) for p in pats)
        if n:
            severe[name] = n
    emails = {e for e in EMAIL_RE.findall(text)
              if e.lower() not in PLACEHOLDER_EMAIL
              and not e.lower().endswith('@example.com')}
    return severe, private, emails, warn


def fix_text(text, rules):
    """把公网 IP 与外部规则字面量替换为占位符。"""
    n = 0
    for ip in set(IPV4_RE.findall(text)):
        if not is_private_or_doc(ip):
            text = text.replace(ip, '<PUBLIC_IP>')
            n += 1
    for name, (pats, ph) in rules.items():
        for p in pats:
            c = text.count(p)
            if c:
                text = text.replace(p, ph)
                n += c
    return text, n


def iter_local(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if fn in SKIP_NAMES:
                continue
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            yield os.path.join(dirpath, fn)


def iter_github(repo, token):
    api = 'https://api.github.com/repos/' + repo
    hdrs = {'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'sanitize-check'}
    req = urllib.request.Request(api + '/git/trees/main?recursive=1', headers=hdrs)
    with urllib.request.urlopen(req, timeout=60) as r:
        tree = json.loads(r.read().decode('utf-8'))
    for b in tree.get('tree', []):
        if b.get('type') != 'blob':
            continue
        p = b['path']
        if os.path.splitext(p)[1].lower() not in TEXT_EXT:
            continue
        try:
            rq = urllib.request.Request(
                api + '/contents/' + urllib.parse.quote(p), headers=hdrs)
            with urllib.request.urlopen(rq, timeout=60) as rr:
                meta = json.loads(rr.read().decode('utf-8'))
            yield p, base64.b64decode(meta['content']).decode('utf-8', 'replace')
        except Exception as e:
            print('  [跳过] %s (%s)' % (p, repr(e)[:60]), file=sys.stderr)


def load_rules(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as fh:
        raw = json.load(fh)
    out = {}
    for k, v in raw.items():
        pats = v if isinstance(v, list) else [v]
        out[k] = (pats, '<' + re.sub(r'[^A-Za-z0-9]', '_', k.upper()) + '>')
    return out


def main():
    ap = argparse.ArgumentParser(description='脱敏校验器（模式识别，不存字面量）')
    ap.add_argument('--dir', help='本地目录')
    ap.add_argument('--repo', help='GitHub 仓库 owner/name')
    ap.add_argument('--token', default=os.environ.get('GH_TOKEN', ''))
    ap.add_argument('--fix', action='store_true')
    ap.add_argument('--rules', help='外部字面量规则 JSON（勿提交）')
    ap.add_argument('--strict', action='store_true', help='内网 IP 也判失败')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    if not args.dir and not args.repo:
        ap.error('需指定 --dir 或 --repo')
    rules = load_rules(args.rules)

    severe, private_all, emails, scanned = {}, [], set(), 0
    detail, warns = {}, {}
    fixed_total, fixed_files = 0, []

    def handle(path, text, writable):
        nonlocal fixed_total
        sev, pri, em, wn = scan_text(text, rules)
        for k, v in wn.items():
            warns[k] = warns.get(k, 0) + v
        if args.fix and writable and sev:
            new, n = fix_text(text, rules)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(new)
            fixed_total += n
            fixed_files.append(path)
            sev, pri, em, wn = scan_text(new, rules)
        for k, v in sev.items():
            severe[k] = severe.get(k, 0) + (len(v) if isinstance(v, list) else v)
            detail.setdefault(k, []).append((path, v))
        for ip in pri:
            private_all.append((path, ip))
        for e in em:
            emails.add((path, e))

    if args.dir:
        for path in iter_local(args.dir):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    text = fh.read()
            except Exception:
                continue
            scanned += 1
            handle(path, text, True)
    else:
        if not args.token:
            ap.error('--repo 模式需 --token 或 GH_TOKEN')
        for path, text in iter_github(args.repo, args.token):
            scanned += 1
            handle(path, text, False)

    private_hits = [p for p in private_all]
    clean = (not severe) and (not emails) and (not (args.strict and private_hits))

    if args.json:
        print(json.dumps({'scanned': scanned, 'clean': clean,
                          'severe': severe, 'private': private_hits[:50],
                          'emails': sorted(emails), 'fixed': fixed_total},
                         ensure_ascii=False, indent=2))
        return 0 if clean else 1

    print('=' * 62)
    print(' 脱敏校验报告（模式识别）')
    print('=' * 62)
    print(' 范围     : %s' % (args.dir or ('GitHub ' + args.repo)))
    print(' 扫描文件 : %d' % scanned)
    print(' 外部规则 : %d 条' % len(rules))
    print('-' * 62)
    if args.fix:
        print(' 自动修复 : %d 处（%d 个文件）' % (fixed_total, len(fixed_files)))
        for p in fixed_files[:8]:
            print('     -', p)
        print('-' * 62)
    if severe:
        print(' ❌ 严重（会导致失败）：')
        for k in sorted(severe):
            print('   [%s]' % k)
            for p, v in detail.get(k, [])[:8]:
                print('       %s -> %s' % (p, v if not isinstance(v, list) else ','.join(v[:5])))
    else:
        print(' ✅ 无公网 IP / 密钥 / 私钥 / 口令泄漏')
    if private_hits:
        tag = '❌' if args.strict else '⚠️'
        print(' %s 内网 IP（%s）：' % (tag, 'strict 视为失败' if args.strict else '仅提示'))
        for p, ip in private_hits[:8]:
            print('       %s -> %s' % (p, ip))
    else:
        print(' ✅ 无内网 IP')
    if emails:
        print(' ⚠️ 非占位邮箱：')
        for p, e in sorted(emails)[:8]:
            print('     %s -> %s' % (p, e))
    else:
        print(' ✅ 邮箱均为占位符')
    if warns:
        print(' ℹ️ 提示（不判失败）：')
        for k in sorted(warns):
            print('     [%s] %d 处' % (k, warns[k]))
    print('=' * 62)
    print(' 结论: %s' % ('干净 CLEAN' if clean else '存在泄漏 LEAK'))
    return 0 if clean else 1


if __name__ == '__main__':
    sys.exit(main())
