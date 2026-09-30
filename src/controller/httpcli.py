# -*- coding: utf-8 -*-
"""内部 HTTP 客户端：控制面↔worker 全部走这里。
- trust_env=False：内部地址绝不经过任何环境代理；
- 对空响应/连接错误自动重试，消除冷连接/并发下的瞬时失败。
"""
import time
import requests

SESSION = requests.Session()
SESSION.trust_env = False


class EmptyResponse(Exception):
    pass


def request(method, url, retries=3, **kw):
    kw.setdefault('timeout', 20)
    # 预置一个异常对象：retries<=0 时循环不执行，原实现会 `raise None`
    # 抛 TypeError，把真实原因掩盖掉。
    last = EmptyResponse('no attempt made')
    for attempt in range(max(1, int(retries))):
        try:
            r = SESSION.request(method, url, **kw)
        except requests.RequestException as e:
            # 连接/超时：可重试
            last = e
            time.sleep(0.2 * (attempt + 1)); continue
        # 4xx：客户端错误，重试无意义，原样返回由调用方处理
        if 400 <= r.status_code < 500:
            return r
        # 5xx：服务端瞬时错误，可重试
        if r.status_code >= 500:
            last = EmptyResponse(f'status={r.status_code}')
            time.sleep(0.2 * (attempt + 1)); continue
        if r.status_code == 200 and (r.content or r.text):
            return r
        last = EmptyResponse(f'status={r.status_code} body-empty')
        time.sleep(0.2 * (attempt + 1))
    raise last


def get(url, **kw):
    return request('GET', url, **kw)


def post(url, **kw):
    return request('POST', url, **kw)
