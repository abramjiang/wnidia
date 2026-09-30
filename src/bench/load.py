# -*- coding: utf-8 -*-
"""WNIDIA 同口径压测：对 OpenAI 兼容端点发压，统计吞吐/延迟/完成数/利用率。
用法：
  # 单端点
  python bench/load.py --target http://127.0.0.1:9000 --token changeme \
      --requests 40 --concurrency 6
  # before/after 对比（同 prompt/并发/请求数）
  python bench/load.py --compare \
      --baseline-url http://127.0.0.1:8000 \
      --wnidia-url  http://127.0.0.1:9000 --token changeme \
      --requests 60 --concurrency 8
"""
import argparse
import json
import statistics
import threading
import time
import requests

S = requests.Session(); S.trust_env = False


def _one(target, token, prompt):
    url = f'{target}/v1/chat/completions'
    t0 = time.time()
    try:
        r = S.post(url, json={'messages': [{'role': 'user', 'content': prompt}]},
                   headers={'Authorization': f'Bearer {token}'}, timeout=60)
        r.raise_for_status()
        return {'ok': True, 'lat': time.time() - t0,
                'tokens': r.json().get('usage', {}).get('total_tokens', 0)}
    except requests.RequestException:
        return {'ok': False, 'lat': time.time() - t0, 'tokens': 0}


def _sample_util(ctrl, token, samples, stop):
    while not stop.is_set():
        try:
            r = S.get(f'{ctrl}/admin/state',
                      headers={'Authorization': f'Bearer {token}'}, timeout=5)
            nodes = r.json()['nodes']
            on = [n for n in nodes if n['status'] == 'online']
            if on:
                samples.append(sum(n['util'] for n in on) / len(on))
        except requests.RequestException:
            pass
        time.sleep(0.5)


def run_load(target, token, n, conc, prompt, ctrl=None):
    lat, ok_n, fail_n, toks = [], 0, 0, 0
    lock = threading.Lock()
    util_samples, stop = [], threading.Event()
    if ctrl:
        threading.Thread(target=_sample_util,
                         args=(ctrl, token, util_samples, stop),
                         daemon=True).start()

    def worker():
        nonlocal ok_n, fail_n, toks
        r = _one(target, token, prompt)
        with lock:
            lat.append(r['lat'])
            if r['ok']:
                ok_n += 1; toks += r['tokens']
            else:
                fail_n += 1

    t0 = time.time()
    threads = []
    for i in range(n):
        t = threading.Thread(target=worker)
        threads.append(t); t.start()
        if len(threads) >= conc:
            for x in threads:
                x.join()
            threads = []
    for x in threads:
        x.join()
    wall = time.time() - t0
    stop.set()
    lat.sort()

    def pct(p):
        return round(lat[min(int(len(lat) * p), len(lat) - 1)], 3) if lat else 0
    return {
        'target': target, 'requests': n, 'concurrency': conc,
        'completed': ok_n, 'failed': fail_n,
        'wall_s': round(wall, 2),
        'throughput_rps': round(ok_n / wall, 2) if wall else 0,
        'tokens_per_s': round(toks / wall, 2) if wall else 0,
        'latency_s': {'p50': pct(0.5), 'p90': pct(0.9), 'p95': pct(0.95),
                      'mean': round(statistics.mean(lat), 3) if lat else 0},
        'avg_util_pct': round(statistics.mean(util_samples), 1)
        if util_samples else None,
    }


def compare(base_url, wn_url, token, n, conc, prompt):
    before = run_load(base_url, token, n, conc, prompt)
    after = run_load(wn_url, token, n, conc, prompt, ctrl=wn_url)
    lift = {}
    if before['throughput_rps']:
        lift['throughput_lift_pct'] = round(
            (after['throughput_rps'] / before['throughput_rps'] - 1) * 100, 1)
    if before['latency_s']['p50']:
        lift['p50_latency_change_pct'] = round(
            (after['latency_s']['p50'] / before['latency_s']['p50'] - 1) * 100, 1)
    return {'before': before, 'after': after, 'lift': lift}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default='http://127.0.0.1:9000')
    ap.add_argument('--baseline-url', default=None)
    ap.add_argument('--wnidia-url', default='http://127.0.0.1:9000')
    ap.add_argument('--token', default='changeme')
    ap.add_argument('--requests', type=int, default=40)
    ap.add_argument('--concurrency', type=int, default=6)
    ap.add_argument('--prompt', default='请简要解释 GPU 异构算力调度的价值')
    ap.add_argument('--compare', action='store_true')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    if a.compare:
        if not a.baseline_url:
            raise SystemExit('--compare 需要 --baseline-url')
        res = compare(a.baseline_url, a.wnidia_url, a.token, a.requests,
                      a.concurrency, a.prompt)
    else:
        res = run_load(a.target, a.token, a.requests, a.concurrency, a.prompt,
                       ctrl=a.target)
    out = json.dumps(res, ensure_ascii=False, indent=2)
    print(out)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(out)


if __name__ == '__main__':
    main()
