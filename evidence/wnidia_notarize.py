#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNIDIA 证据固化脚本：确定性打包 → 计算 SHA256 → 提交可信时间戳

用途：为代码 / 文档生成法律级"存在性证明"（证明某个时间点某内容已存在且未被篡改）。

用法：
  # 模式 A：打包整个目录（推荐，用于源码快照）
  python3 wnidia_notarize.py --dir /path/to/src --out ./evidence

  # 模式 B：直接对单个文件（如发布包 zip）取哈希
  python3 wnidia_notarize.py --file wnidia_v5.1_gx10_r9.zip --out ./evidence

  # 接入国内可信时间戳（RFC3161，需先注册获取接口地址）
  TSA_URL=https://your-tsa.example/tsa python3 wnidia_notarize.py --dir ./src --out ./evidence

产出（在 --out 目录）：
  <name>.tar.gz        确定性打包产物
  <name>.sha256        整体 SHA256
  <name>.manifest.txt  逐文件 SHA256 清单
  <name>.ots           OpenTimestamps 链上时间戳凭证（比特币锚定，免费）
  <name>.tsq/.tsr      RFC3161 时间戳请求/响应（需 TSA_URL）
  EVIDENCE_README.txt  验证与使用方法说明

确定性保证：文件名按字节序排序、mtime 固定为 0、uid/gid 归零、权限归一化、
排除运行时产物 —— 任何人重新打包都将得到 **完全相同** 的 SHA256。
"""

import argparse
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import time

# 打包时排除：运行时产物 / 缓存 / 版本库元数据
EXCLUDE_DIRS = {'.git', '.venv', '__pycache__', 'node_modules', '.mypy_cache',
                '.pytest_cache', '.idea', '.vscode'}
EXCLUDE_SUFFIX = ('.pyc', '.pyo', '.db-wal', '.db-shm', '.log', '.pid', '.tmp', '.swp')
EXCLUDE_NAMES = {'.DS_Store'}

OTS_CALENDARS = [
    'https://a.pool.opentimestamps.org',
    'https://b.pool.opentimestamps.org',
    'https://alice.btc.calendar.opentimestamps.org',
]


def should_skip(rel_path, fn):
    parts = rel_path.split(os.sep)
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if fn in EXCLUDE_NAMES:
        return True
    return fn.endswith(EXCLUDE_SUFFIX)


def collect_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if should_skip(rel, fn):
                continue
            out.append((rel, full))
    out.sort(key=lambda x: x[0].encode('utf-8'))  # 字节序排序，保证确定性
    return out


def make_deterministic_tar(root, tar_path):
    """确定性打包：文件字节序排序 + mtime/uid/gid/权限固定 + gzip 头 mtime 归零。

    gzip 头部的 mtime 字段默认是"当前时间"，若不归零会导致每次打包哈希都不同，
    时间戳将失去法律意义 —— 这里显式置 0。
    """
    files = collect_files(root)
    raw = open(tar_path, 'wb')
    gz = gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0)
    tf = tarfile.open(fileobj=gz, mode='w', format=tarfile.GNU_FORMAT)
    try:
        for rel, full in files:
            ti = tf.gettarinfo(full, arcname=rel)
            ti.mtime = 0            # 固定时间戳
            ti.uid = 0
            ti.gid = 0
            ti.uname = ''
            ti.gname = ''
            ti.mode = 0o644 if os.path.isfile(full) else 0o755
            with open(full, 'rb') as fh:
                tf.addfile(ti, fh)
    finally:
        tf.close()
        gz.close()
        raw.close()
    return len(files)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(files, root, manifest_path, tar_sha, src_desc):
    lines = [
        '# WNIDIA 证据清单 (SHA256 manifest)',
        '# 生成时间(UTC): %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        '# 源: %s' % src_desc,
        '# 打包产物整体 SHA256: %s' % tar_sha,
        '',
    ]
    for rel, full in files:
        lines.append('%s  %s' % (sha256_file(full), rel.replace(os.sep, '/')))
    with open(manifest_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


def try_opentimestamps(target, out_dir, name):
    """优先用 ots CLI；否则走 HTTP 日历服务器。返回说明字符串。"""
    # 1) ots CLI
    if shutil.which('ots'):
        try:
            r = subprocess.run(['ots', 'stamp', target], capture_output=True, timeout=120)
            if r.returncode == 0:
                return 'OpenTimestamps: 已用 ots CLI 生成 %s.ots' % name
        except Exception as e:
            return 'OpenTimestamps: ots CLI 执行失败 (%s)' % e

    # 2) HTTP 日历服务器：POST 原始 32 字节摘要
    digest = hashlib.sha256(open(target, 'rb').read()).digest()
    import urllib.request
    for cal in OTS_CALENDARS:
        for path in ('/digest', '/'):
            try:
                req = urllib.request.Request(cal + path, data=digest, method='POST',
                                             headers={'User-Agent': 'wnidia-notarize',
                                                      'Content-Type': 'application/octet-stream'})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    blob = resp.read()
                if blob:
                    with open(os.path.join(out_dir, name + '.ots'), 'wb') as fh:
                        fh.write(blob)
                    return 'OpenTimestamps: 已从 %s%s 取得凭证 %s.ots（待 Bitcoin 确认后升级）' % (cal, path, name)
            except Exception:
                continue
    return 'OpenTimestamps: 未成功（可能无外网 / 未装 ots）。稍后可手动执行：ots stamp <file>'


def try_rfc3161(target, out_dir, name, tsa_url):
    """用 openssl ts 走 RFC3161 时间戳（国内可信时间戳服务即此协议）。"""
    if not tsa_url:
        return 'RFC3161: 未提供 TSA_URL，已跳过。'
    if not shutil.which('openssl'):
        return 'RFC3161: 未找到 openssl，已跳过。'
    tsq = os.path.join(out_dir, name + '.tsq')
    tsr = os.path.join(out_dir, name + '.tsr')
    try:
        subprocess.run(['openssl', 'ts', '-query', '-data', target, '-sha256',
                        '-no_nonce', '-out', tsq], check=True, capture_output=True)
        subprocess.run(['curl', '-sS', '-H', 'Content-Type: application/timestamp-query',
                        '--data-binary', '@' + tsq, '-o', tsr, tsa_url],
                       check=True, capture_output=True, timeout=120)
        # 校验响应
        v = subprocess.run(['openssl', 'ts', '-reply', '-in', tsr, '-queryfile', tsq, '-token_in'],
                           capture_output=True)
        if v.returncode == 0:
            return 'RFC3161: 已取得时间戳响应 %s.tsr（openssl 校验通过）' % name
        return 'RFC3161: 取得响应但校验未通过，请检查 TSA 地址与凭据。'
    except Exception as e:
        return 'RFC3161: 失败 (%s)。请确认 TSA_URL 可用且需鉴权参数。' % e


def main():
    ap = argparse.ArgumentParser(description='WNIDIA 证据固化：打包 + SHA256 + 可信时间戳')
    ap.add_argument('--dir', help='要打包的源目录')
    ap.add_argument('--file', help='或直接对单个文件取哈希')
    ap.add_argument('--out', default='./evidence', help='证据输出目录')
    ap.add_argument('--name', default=None, help='产物文件名前缀')
    args = ap.parse_args()

    if not args.dir and not args.file:
        ap.error('需指定 --dir 或 --file')

    os.makedirs(args.out, exist_ok=True)
    tsa_url = os.environ.get('TSA_URL', '').strip()
    name = args.name or ('wnidia_snapshot_%s' % time.strftime('%Y%m%d'))

    if args.dir:
        src = os.path.abspath(args.dir)
        tar_path = os.path.join(args.out, name + '.tar.gz')
        n = make_deterministic_tar(src, tar_path)
        target = tar_path
        src_desc = src
        files = collect_files(src)
        tar_sha = sha256_file(tar_path)
        write_manifest(files, src, os.path.join(args.out, name + '.manifest.txt'), tar_sha, src_desc)
        print('打包: %s（%d 个文件）' % (tar_path, n))
    else:
        src = os.path.abspath(args.file)
        target = src
        tar_sha = sha256_file(src)
        shutil.copy2(src, os.path.join(args.out, os.path.basename(src)))
        print('文件: %s' % src)

    print('SHA256: %s' % tar_sha)

    with open(os.path.join(args.out, name + '.sha256'), 'w') as fh:
        fh.write('%s  %s\n' % (tar_sha, os.path.basename(target)))

    print(try_opentimestamps(target, args.out, name))
    print(try_rfc3161(target, args.out, name, tsa_url))

    readme = """WNIDIA 证据包 · 验证说明
================================================
生成时间(UTC): %(now)s
源: %(src)s
打包/目标文件 SHA256:
  %(sha)s

一、确定性校验（任何人可复现）
------------------------------------------------
在任意机器上对同一目录重新打包，应得到完全相同的 SHA256。
若不同，说明文件内容已被改动。

  shasum -a 256 <打包文件>

二、OpenTimestamps（比特币锚定，免费）
------------------------------------------------
凭证文件: %(name)s.ots
OTS 凭证在生成后需等待比特币网络确认（通常数小时），之后执行：

  ots upgrade %(name)s.ots     # 升级为完整证明
  ots verify  %(name)s.ots     # 验证（应看到 Bitcoin block height）

未装 ots 可安装：pip install opentimestamps-client

三、RFC3161 可信时间戳（国内司法认可度高）
------------------------------------------------
若已配置 TSA_URL，会生成 %(name)s.tsq（请求）与 %(name)s.tsr（响应）。
验证：

  openssl ts -verify -in %(name)s.tsr -queryfile %(name)s.tsq -CAfile <TSA证书>

国内常用可信时间戳服务（需注册获取接口地址与证书）：
  - 联合信任时间戳服务中心（www.tsa.cn）
  - 各省市 CA / 电子认证服务机构提供的 RFC3161 接口
接入方式：以环境变量传入接口地址后重跑本脚本
  TSA_URL=https://your-tsa/tsa python3 wnidia_notarize.py --dir ... --out ...

四、法律要点
------------------------------------------------
- 本证据包证明「该 SHA256 对应的内容在时间戳所示时刻已存在」。
- 时间戳 + 内容哈希 = 存在性与完整性证明；配合 GitHub 公开时间线，
  可形成完整的原创性证据链。
- 建议将证据包（含 .ots / .tsr）与 GitHub Release 一并归档保存。
""" % {'now': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
       'src': src, 'sha': tar_sha, 'name': name}

    # 按 name 命名，避免同一输出目录多次运行时互相覆盖
    with open(os.path.join(args.out, '%s_EVIDENCE_README.txt' % name), 'w', encoding='utf-8') as fh:
        fh.write(readme)

    print('证据包已输出到:', os.path.abspath(args.out))


if __name__ == '__main__':
    main()
