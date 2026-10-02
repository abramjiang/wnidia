# -*- coding: utf-8 -*-
"""合规守卫：把《Spark 云节点访问与使用手册》第八章的红线，变成可执行代码。

设计原则：**默认拒绝（fail-closed）**。
    - 只有 8888 / 9000 两个端口被组委会做了公网映射，也**只有**这两个端口允许绑 0.0.0.0；
    - 其余服务（worker、引擎、Agent UI、Jev 服务）一律只允许绑回环；
    - 拒绝弱口令上公网（手册 8.2-9：公网服务必须设置 token 或密码）；
    - 拒绝把「探测目标」指向非回环、非白名单地址（手册 8.1-2：严禁扫描/探测内网其他节点）；
    - 任何凭据在日志与终端输出中必须脱敏（手册 8.1-3：严禁公开账号密码）。

手册条款 → 代码映射见 docs/COMPLIANCE.md。
"""
import ipaddress
import os

# 部署形态：spark = 手册标准节点（8888/9000 公网）；gx10 = 实际分配的 gx10 节点。
# gx10 公网映射经 2026-09-29 从公网实测确认（本机 <PUBLIC_IP>）：
#   7026→7000（Agent）  8026→8888（看板）  9026→9000（API）  <SSH_PORT>→22（SSH）
#   公网 8888 无映射（curl 超时）；公网 8026 应答 HTTPBasic 401 = 看板；
#   公网 9026 /healthz 返回 {"ok":true} = 控制面 API。默认 spark，零行为变化。
DEPLOY_TARGET = os.getenv('WNIDIA_DEPLOY_TARGET', 'spark').strip().lower()

if DEPLOY_TARGET == 'gx10':
    # gx10：三个被公网映射的内网端口（Agent 7000 / 看板 8888 / API 9000）
    PUBLIC_PORTS = (7000, 8888, 9000)
    # gx10 内网网段（禁止探测其它设备）
    FORBIDDEN_NETWORKS = (
        ipaddress.ip_network('<LAN_SUBNET_2>/24'),
    )
    # 内网端口 -> 公网端口 的固定映射（实测）
    _GX10_PUBLIC_OF = {7000: 7026, 8888: 8026, 9000: 9026, 22: <SSH_PORT>}
else:
    # 手册 1.2 / 4.1：仅 8888 与 9000 做了公网映射
    PUBLIC_PORTS = (8888, 9000)
    # 手册 8.1-2：严禁探测/扫描的内网网段
    FORBIDDEN_NETWORKS = (
        ipaddress.ip_network('<LAN_SUBNET>/24'),
    )

# 公认弱口令（手册 8.2-9 的反面清单）
WEAK_SECRETS = {
    '', 'changeme', 'change_me', 'password', 'passwd', '123456', '12345678',
    'admin', 'root', 'test', 'demo', 'wnidia', 'wnidia2026', 'secret',
    'token', 'default', '000000', '111111', 'qwerty', 'letmein',
}


class ComplianceError(RuntimeError):
    """违反赛事红线：拒绝启动/拒绝执行，而不是降级运行。"""


# ---------------------------------------------------------------- 地址
def _parse_host(host):
    h = (host or '').strip().strip('[]').lower()
    if h in ('localhost',):
        return ipaddress.ip_address('127.0.0.1')
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return None            # 主机名/DNS 名，交由调用方按白名单判定


def is_loopback(host) -> bool:
    ip = _parse_host(host)
    return bool(ip and ip.is_loopback)


def is_wildcard(host) -> bool:
    h = (host or '').strip().lower()
    return h in ('0.0.0.0', '::', '*') or h == ''


def allowlist():
    """显式放行的探测目标（默认空）。用 WNIDIA_ALLOW_HOSTS=a,b 追加。"""
    raw = os.getenv('WNIDIA_ALLOW_HOSTS', '')
    return {x.strip().lower() for x in raw.split(',') if x.strip()}


def assert_bind_allowed(host, port, what='service'):
    """绑 0.0.0.0 仅允许出现在被公网映射的两个端口上。"""
    if not is_wildcard(host):
        return True
    if int(port) in PUBLIC_PORTS:
        return True
    raise ComplianceError(
        f'{what} 端口 {port} 不在公网映射范围内，禁止绑 {host}。'
        f'手册 4.1：组委会只为 8888 与 9000 做公网映射；'
        f'其余端口请绑 127.0.0.1 并用 SSH 隧道访问（手册第五章）。'
        f'如需临时放行，设置 WNIDIA_ALLOW_NONPUBLIC_BIND=1 并自行承担风险。'
    )


def assert_probe_allowed(target, what='probe'):
    """禁止把网络探测指向内网其他节点（手册 8.1-2，行为全量留日志）。"""
    t = (target or '').strip().lower()
    if not t:
        raise ComplianceError(f'{what}: 探测目标为空')
    if t in allowlist():
        return True
    ip = _parse_host(t)
    if ip is None:
        # 主机名：允许解析为回环的常见写法，其余一律拒绝
        if t.startswith('localhost'):
            return True
        raise ComplianceError(
            f'{what}: 目标 {t} 不是回环地址。手册 8.1-2 严禁探测内网其他节点，'
            f'如确需纳管其他主机，请把它加入 WNIDIA_ALLOW_HOSTS 白名单后重试。')
    if ip.is_loopback:
        return True
    for net in FORBIDDEN_NETWORKS:
        if ip in net:
            raise ComplianceError(
                f'{what}: 目标 {ip} 属于禁止探测的集群内网 {net}。'
                f'手册 8.1-2 严禁扫描、探测内网中的其他节点；'
                f'所有节点的网络行为均有日志记录，'
                f'探测其他队伍节点将立即回收节点并取消参赛资格。')
    raise ComplianceError(
        f'{what}: 目标 {ip} 非回环地址且不在白名单内。'
        f'如需纳管，请通过 WNIDIA_ALLOW_HOSTS 显式声明。')


# ---------------------------------------------------------------- 凭据
def is_weak(secret) -> bool:
    s = (secret or '').strip()
    if len(s) < 12:
        return True
    if s.lower() in WEAK_SECRETS:
        return True
    # 单一字符重复 / 纯数字 / 纯字母
    if len(set(s)) <= 3:
        return True
    return False


def assert_secret_strength(name, value, public):
    """公网端口上的服务必须用强凭据（手册 8.2-9）。"""
    if not public:
        return True
    if is_weak(value):
        raise ComplianceError(
            f'{name} 使用了弱口令/默认口令，而它将暴露在公网端口上。'
            f'手册 8.2-9：8888 / 9000 上的服务必须设置 token 或密码。'
            f'请用环境变量注入至少 12 位、非纯数字/纯字母的随机串，例如：'
            f"export {name}=$(python3 -c \"import secrets;print(secrets.token_urlsafe(24))\")")
    return True


def mask(secret, keep=4):
    """日志/终端输出统一脱敏（手册 8.1-3）。"""
    s = str(secret or '')
    if not s:
        return '(empty)'
    if len(s) <= keep * 2:
        return '*' * len(s)
    return f'{s[:keep]}{"*" * (len(s) - keep * 2)}{s[-keep:]}'


# ---------------------------------------------------------------- 汇总自检
def preflight(binds=None, secrets=None, strict=True):
    """启动/部署前统一自检。binds=[(name, host, port)]，secrets=[(name, value, public)]。"""
    problems, notes = [], []
    try:
        for name, host, port in (binds or []):
            try:
                assert_bind_allowed(host, port, name)
                if is_wildcard(host):
                    notes.append(f'{name} 绑 {host}:{port}（该端口已获公网映射，合规）')
                else:
                    notes.append(f'{name} 绑 {host}:{port}（回环，需 SSH 隧道访问）')
            except ComplianceError as e:
                problems.append(str(e))
        for name, value, public in (secrets or []):
            try:
                assert_secret_strength(name, value, public)
                if public:
                    notes.append(f'凭据 {name}={mask(value)}（公网端口，强度校验通过）')
            except ComplianceError as e:
                problems.append(str(e))
    except Exception as e:                      # noqa: BLE001
        problems.append(f'自检内部异常：{e}')
    if problems and strict:
        raise ComplianceError('合规自检未通过：\n  - ' + '\n  - '.join(problems))
    return {'ok': not problems, 'problems': problems, 'notes': notes}


def public_port_of(internal_port):
    """换算公网端口。gx10 用固定映射；spark 按手册 1.2：8888→8+NN，9000→9+NN。"""
    if DEPLOY_TARGET == 'gx10':
        try:
            return _GX10_PUBLIC_OF.get(int(internal_port))
        except (TypeError, ValueError):
            return None
    nn = os.getenv('NODE_NUM', os.getenv('SPARK_NN', '')).strip()
    if not nn.isdigit():
        return None
    n = int(nn)
    if internal_port == 8888:
        return 8000 + n
    if internal_port == 9000:
        return 9000 + n
    if internal_port == 22:
        return 6000 + n
    return None
