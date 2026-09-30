#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WNIDIA 演示流量发生器（traffic generator）。

用途：真实调度系统没有"模拟数据发生器"，零在途任务时看板读数必然静止
（util=0、进度不动、事件流不增长）。本脚本按并发度持续提交真实任务，
让看板/事件流/GPU 面板呈现真实的调度过程——不是伪造数据，是真的在跑。

用法（节点本机）：
    python3 scripts/traffic_gen.py --base http://127.0.0.1:9000 --token <T> \
        --concurrency 3 --duration 120

用法（从你的电脑打公网）：
    python3 scripts/traffic_gen.py --base http://<PUBLIC_IP>:9026 --token <T> \
        --concurrency 3 --duration 120

参数：
    --concurrency N   并发路数（默认 3，对应 cloud/edge/cpu 三个 worker）
    --duration S      运行时长秒，0=直到 Ctrl-C（默认 0）
    --max-tokens N    每条请求最大生成长度（默认 128，越小越快）
    --interval F      同一路两次请求之间的间隔秒（默认 0.5）
    --model M         模型名（默认取节点上已 pull 的 Qwen3.8-27B-GGUF）
    --dry-run         只打印将要做什么，不真发请求

零依赖（只用标准库 urllib），节点与本机都能直接跑。
"""
import argparse
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

PROMPTS = [
    '用一句话说明边缘算力箱适合跑什么任务',
    '解释 GPU 利用率上不去最常见的三个原因',
    '显存不够时，量化与切分应该先选哪个',
    '为什么同一模型在不同节点上时延差很多',
    '一句话说明抢占式调度适合什么业务',
    '异构算力池里 CPU 节点还有存在价值吗',
    '简述 KV cache 命中率对吞吐的影响',
    '离线边缘节点断网时如何保证任务不丢',
    '如何判断一个节点上报的算力数据是否可信',
    '单位算力的成本与能耗，哪个更该优先优化',
]

DEFAULT_MODEL = 'modelscope.cn/unsloth/Qwen3.8-27B-GGUF:latest'

_LOCK = threading.Lock()
_STAT = {'ok': 0, 'fail': 0, 'lat': [], 'inflight': 0}


def post(base, token, model, prompt, max_tokens, timeout):
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
    }).encode('utf-8')
    req = urllib.request.Request(
        base.rstrip('/') + '/v1/chat/completions', data=body,
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + token},
        method='POST')
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode('utf-8', 'replace')
        j = json.loads(raw)
        ans = j.get('choices', [{}])[0].get('message', {}).get('content', '')
        return time.time() - t0, len(ans), None
    except urllib.error.HTTPError as e:
        return time.time() - t0, 0, 'HTTP %s: %s' % (e.code, e.read()[:120])
    except Exception as e:                       # noqa: BLE001
        return time.time() - t0, 0, type(e).__name__ + ': ' + str(e)[:80]


def worker(idx, args):
    """每一路持续提交，直到时间到或被中断。"""
    n = 0
    while not args._stop.is_set():
        prompt = PROMPTS[(idx + n) % len(PROMPTS)]
        with _LOCK:
            _STAT['inflight'] += 1
        lat, chars, err = post(args.base, args.token, args.model, prompt,
                               args.max_tokens, args.timeout)
        with _LOCK:
            _STAT['inflight'] -= 1
            if err:
                _STAT['fail'] += 1
            else:
                _STAT['ok'] += 1
                _STAT['lat'].append(lat)
            i = _STAT['inflight']
        flag = 'ERR' if err else 'ok'
        print('  [路%d] %s  %.1fs  %d字  在途=%d  %s'
              % (idx, flag, lat, chars, i, (err or '')[:60]))
        n += 1
        if args.interval:
            time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser(description='WNIDIA 演示流量发生器')
    ap.add_argument('--base', default=os.getenv('CTRL', 'http://127.0.0.1:9000'))
    ap.add_argument('--token', default=os.getenv('WNIDIA_TOKEN', ''))
    ap.add_argument('--concurrency', type=int, default=3)
    ap.add_argument('--duration', type=float, default=0, help='0=直到 Ctrl-C')
    ap.add_argument('--interval', type=float, default=0.5)
    ap.add_argument('--max-tokens', type=int, default=128)
    ap.add_argument('--timeout', type=int, default=180)
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not args.token:
        print('[error] 缺 --token（或用环境变量 WNIDIA_TOKEN）')
        return 2
    if args.dry_run:
        print('[dry-run] base=%s 并发=%d 时长=%ss 间隔=%ss max_tokens=%d'
              % (args.base, args.concurrency, args.duration or '∞',
                 args.interval, args.max_tokens))
        return 0

    print('流量发生器启动：%s  并发=%d  时长=%ss'
          % (args.base, args.concurrency, args.duration or '∞'))
    print('（Ctrl-C 停止；停止后已完成任务仍会留在看板历史里）')

    args._stop = threading.Event()
    ths = [threading.Thread(target=worker, args=(i, args), daemon=True)
           for i in range(args.concurrency)]
    for t in ths:
        t.start()
    t0 = time.time()
    try:
        while True:
            time.sleep(0.5)
            if args.duration and time.time() - t0 >= args.duration:
                break
    except KeyboardInterrupt:
        print('\n收到 Ctrl-C，正在停止...')
    args._stop.set()
    for t in ths:
        t.join(timeout=args.timeout + 5)

    el = max(time.time() - t0, 0.1)
    with _LOCK:
        ok, fail = _STAT['ok'], _STAT['fail']
        lats = _STAT['lat']
    avg = sum(lats) / len(lats) if lats else 0
    print('\n=== 汇总 ===')
    print(' 成功 %d / 失败 %d  平均时延 %.1fs  吞吐 %.2f 次/秒  用时 %.0fs'
          % (ok, fail, avg, ok / el, el))
    return 0


if __name__ == '__main__':
    sys.exit(main())
