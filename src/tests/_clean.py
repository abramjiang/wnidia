# -*- coding: utf-8 -*-
"""测试基础设施：端口清场与进程组回收。

为什么需要它（BUG-25）：
    1. `run_local.sh` 的 SIGTERM 陷阱会让 bash 先退出；随后测试里的
       `os.killpg(os.getpgid(proc.pid), SIGKILL)` 因为 pid 已被回收而抛
       `ProcessLookupError`，被 `except` 吞掉 —— **SIGKILL 从未发出**，
       uvicorn/worker 子进程残留，端口一直被占。
    2. 不清场就启动新实例时，新测试用新 Token 打旧实例 → 401 →
       `state()['tasks']` 抛一个与真实原因毫无关系的 KeyError。

识别残留进程的策略（**保守，绝不误杀**）：
    - 只用 `lsof` 取监听者（不用 ps：某些沙箱里 ps 会被拒绝，返回空字符串，
      导致"按命令行特征匹配"完全失效——这正是第一版踩的坑）；
    - 必须同时满足两条才清理：
        ① lsof 报告的 COMMAND 以 python 开头；且
        ② 端口指纹匹配本项目（各端口返回的 JSON 形状/响应头特征）。
    任何一条不满足就**不动它**，并打印可操作提示。
"""
import json
import os
import signal
import subprocess
import sys
import time

import requests

DEFAULT_PORTS = (9000, 8888, 7000, 8101, 8102, 8103, 8104)


# ---------------------------------------------------------------- 抓监听者
def _listeners(ports):
    """返回 {port: (pid, command)}。仅取 LISTEN 状态。

    注意：`-F` 必须带上 `n`（name）字段，否则输出里根本没有端口号，
    解析结果会是空表（第一版就踩了这个坑：`-Fpc` 只有 pid 与 command）。
    """
    out = {}
    try:
        p = subprocess.run(
            ['lsof', '-nP', '-Fpcn',
             '-iTCP:' + ','.join(str(x) for x in ports), '-sTCP:LISTEN'],
            capture_output=True, text=True, timeout=10)
        cmd, pid = None, None
        for line in p.stdout.splitlines():
            if line.startswith('p'):
                pid = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith('c'):
                cmd = line[1:]
            elif line.startswith('n') and pid:
                tail = line.rsplit(':', 1)[-1]
                if tail.isdigit() and int(tail) in ports:
                    out.setdefault(int(tail), (pid, cmd or ''))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return out


# ---------------------------------------------------------------- 端口指纹
def _http(port, path, timeout=1.5):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}',
                                    timeout=timeout) as r:
            return r.status, r.headers, r.read(4096).decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, ''
    except Exception:                       # noqa: BLE001
        return None, None, ''


def _looks_like_wnidia(port):
    """端口指纹：只认本项目服务的响应形状。"""
    if port == 9000:      # 控制面
        st, _, body = _http(port, '/healthz')
        return st == 200 and '"ok"' in body and 'true' in body
    if port == 7000:      # Agent
        st, _, body = _http(port, '/healthz')
        return st == 200 and 'skills' in body
    if port == 8888:      # 看板：401 + Basic 挑战
        st, hdrs, _ = _http(port, '/')
        return st == 401 and 'basic' in (hdrs or {}).get(
            'WWW-Authenticate', '').lower()
    if port in (8101, 8102, 8103, 8104):   # worker
        st, _, body = _http(port, '/healthz')
        return st == 200 and '"node"' in body and '"gpu"' in body
    return False


def diagnose(ports=DEFAULT_PORTS):
    """返回 [{port, pid, command, ours}]，供测试与人工排查共用。"""
    rows = []
    for port, (pid, cmd) in sorted(_listeners(ports).items()):
        rows.append({'port': port, 'pid': pid, 'command': cmd,
                     'ours': cmd.startswith('python') and _looks_like_wnidia(port)})
    return rows


# ---------------------------------------------------------------- 清场
def free_ports(ports=DEFAULT_PORTS, notify=True):
    killed, skipped = [], []
    for row in diagnose(ports):
        if row['pid'] == os.getpid():
            continue
        if not row['ours']:
            skipped.append(row)
            continue
        try:
            os.kill(row['pid'], signal.SIGKILL)
            killed.append(row)
        except OSError:
            skipped.append(row)
    if killed:
        time.sleep(1.2)
    if notify:
        for row in killed:
            print(f"  [clean] 清理本项目残留：pid={row['pid']} "
                  f"port={row['port']} {row['command']}")
        for row in skipped:
            print(f"  [warn] 端口 {row['port']} 被无法确认为本项目的进程占用，"
                  f"未处理：pid={row['pid']} cmd={row['command'] or '?'}")
    return killed, skipped


def preflight_ports(ports=DEFAULT_PORTS):
    """确保端口可用；仍有占用时给出可操作提示并退出（不静默失败）。"""
    free_ports(ports)
    rows = diagnose(ports)
    if rows:
        print('[error] 以下端口仍被占用，无法开始测试：')
        for row in rows:
            print(f"          port={row['port']} pid={row['pid']} "
                  f"cmd={row['command'] or '?'}")
        print('        处理建议： lsof -nP -iTCP:%s -sTCP:LISTEN'
              % ','.join(str(r['port']) for r in rows))
        print('                   kill -9 <上面列出的 pid>')
        sys.exit(2)


def dump_ports():
    print(json.dumps(diagnose(), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 会话
def _is_timeout_error(e):
    """判断一个异常是不是"超时"。

    urllib3 会把 `ReadTimeout` 包成 `ConnectionError(MaxRetryError(...))`，
    所以不能只看最外层类型，必须沿 `__cause__ / __context__` 链走一遍。
    """
    seen, cur = set(), e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, requests.exceptions.Timeout):
            return True
        if 'Timeout' in type(cur).__name__:
            return True
        cur = cur.__cause__ or cur.__context__
    return 'timed out' in str(e)


class _RetryOnReset(requests.Session):
    """只在『连接被服务端重置 / 建连失败』时重试；超时与其它异常原样抛出。

    为什么不用 urllib3 的 Retry 适配器：那会把 `ReadTimeout` 变成
    `MaxRetryError → ConnectionError`，**改变异常类型**。而本仓库有用例
    （`adversarial_test.submit_spot`）**故意**用 1.5s 超时提交一个不等完成的任务，
    靠 `except ReadTimeout` 继续往下跑；一旦类型被改，测试会直接崩。
    """

    def request(self, method, url, **kw):
        last = None
        for i in range(self._attempts):
            try:
                return super().request(method, url, **kw)
            except requests.exceptions.ConnectionError as e:
                last = e
                if _is_timeout_error(e) or i == self._attempts - 1:
                    raise
                time.sleep(0.25 * (i + 1))
        raise last            # pragma: no cover - 循环内必已 raise


def robust_session(attempts=3):
    """带**连接层**重试的 requests 会话（BUG-V4-17）。

    为什么需要：
        控制面某个请求抛未处理异常时（例如非法入参导致的 500），uvicorn 会关闭
        该 keep-alive 连接。下一次请求若复用这条池化连接，就会拿到
        `ConnectionResetError: Connection reset by peer`；而 requests 默认
        **不对 POST 重试**，于是整个测试进程直接崩掉 —— 真实断言结果被掩盖，
        看起来像"被测服务挂了"，其实只是连接复用踩到了上一次的失败。

    只重试连接层，**不重试任何状态码**：非幂等请求不能因为服务端报错被重复提交。
    """
    s = _RetryOnReset()
    s._attempts = max(1, int(attempts))
    s.trust_env = False
    return s


# ---------------------------------------------------------------- 进程组
def pgid_of(proc):
    """进程启动后立刻调用一次并保存：之后 pid 可能被回收，getpgid 会抛异常。"""
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return None


def kill_tree(pgid, proc=None, grace=1.0):
    """先 SIGTERM 再 SIGKILL，**SIGKILL 一定会发出**（不因 getpgid 失败而跳过）。"""
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(grace)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:      # noqa: BLE001
            pass
    time.sleep(0.5)


if __name__ == '__main__':
    dump_ports()
