"""出站 URL 安全校验与连接级 IP 绑定传输（修复 P1-01 / 复核 P1-02）。

覆盖：
- 异步 DNS 解析与超时控制（不阻塞事件循环）
- Webhook 目标校验（默认仅允许公网 HTTPS，禁止内网/回环/链路本地/云元数据地址）
- 连接级 IP 固定（PinnedIPNetworkBackend + PinnedIPAsyncTransport）：实际 TCP 连接强制指向已审批解析 IP，
  彻底杜绝 DNS Rebinding TOCTOU，同时保留原主机名的 TLS SNI 与证书主机名校验，并关闭 trust_env 代理绕过
- 接码平台 Base URL 校验（防止 API Key 外泄）
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import socket
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional
from urllib.parse import urlparse

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from app.config import env
from app.errors import AppError

DEFAULT_UPSTREAM_HOSTS = {
    "smsbower": frozenset({"smsbower.page", "smsbower.online", "smsbower.com"}),
    "herosms": frozenset({"hero-sms.com", "api.hero-sms.com"}),
}


@dataclass(frozen=True)
class _PinnedRoute:
    host: str
    ips: tuple[str, ...]
    allow_private: bool
    allow_loopback: bool = False


_PINNED_WEBHOOK_ROUTE: contextvars.ContextVar[Optional[_PinnedRoute]] = contextvars.ContextVar(
    "sms_hub_pinned_webhook_route",
    default=None,
)


def normalize_url_hostname(host: str, *, error_code: str = "INVALID_WEBHOOK_URL") -> str:
    """规范化主机名：去尾点、小写化、IPv6 去括号、非 ASCII 域名转 IDNA Punycode。"""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".")
    if not h:
        return ""
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    if not h.isascii():
        try:
            h = h.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise AppError(f"URL 主机名包含无效国际化域名字符: {exc}", status_code=422, code=error_code) from exc
    return h


@contextmanager
def pin_webhook_host(
    host: str,
    approved_ips: Iterable[str],
    *,
    allow_private: bool = False,
    allow_loopback: Optional[bool] = None,
) -> Iterator[None]:
    """在当前异步上下文中将目标主机绑定到已批准的解析 IP 列表，供底层 TCP 连接强制使用。"""
    norm_host = normalize_url_hostname(host)
    clean_ips = tuple(str(ip).strip() for ip in approved_ips if str(ip).strip())
    eff_loopback = (
        _host_matches_allowlist(norm_host, env.webhook_allowed_hosts)
        if allow_loopback is None
        else bool(allow_loopback)
    )
    token = _PINNED_WEBHOOK_ROUTE.set(
        _PinnedRoute(host=norm_host, ips=clean_ips, allow_private=allow_private, allow_loopback=eff_loopback)
    )
    try:
        yield
    finally:
        _PINNED_WEBHOOK_ROUTE.reset(token)


def _host_matches_allowlist(host: str, allowlist: Iterable[str]) -> bool:
    h = normalize_url_hostname(host) if host else ""
    if not h:
        return False
    for raw in allowlist:
        raw_str = str(raw or "").strip().lower().rstrip(".")
        if not raw_str:
            continue
        if raw_str.startswith("*."):
            base = normalize_url_hostname(raw_str[2:])
            suffix = f".{base}"
            if base and h.endswith(suffix) and len(h) > len(suffix):
                return True
        else:
            pattern = normalize_url_hostname(raw_str)
            if pattern and h == pattern:
                return True
    return False


def _is_forbidden_even_when_private_allowed(ip_str: str, *, allow_loopback: bool = False) -> bool:
    """即使放行私有网段，也绝不允许链路本地(169.254 云元数据)、未指定(0.0.0.0/::)、组播；回环仅限显式主机白名单。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return True
    if ip.is_loopback and not allow_loopback:
        return True
    return False


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        (not ip.is_global)
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _safe_url_port(parsed, *, error_code: str = "INVALID_WEBHOOK_URL") -> Optional[int]:
    try:
        return parsed.port
    except ValueError as exc:
        raise AppError(f"URL 端口号无效: {exc}", status_code=422, code=error_code) from exc


def _validate_webhook_url_syntax(
    url: str,
    *,
    allowed_project_url: str = "",
    enforce_project_origin: bool = False,
) -> tuple[str, str, int, bool, bool, Optional[str]]:
    raw = (url or "").strip()
    if not raw:
        raise AppError("Webhook URL 不能为空", status_code=422, code="INVALID_WEBHOOK_URL")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    host = normalize_url_hostname(parsed.hostname or "", error_code="INVALID_WEBHOOK_URL")
    parsed_port = _safe_url_port(parsed, error_code="INVALID_WEBHOOK_URL")

    if not host:
        raise AppError("Webhook URL 缺少有效主机名", status_code=422, code="INVALID_WEBHOOK_URL")
    if parsed.username or parsed.password:
        raise AppError("Webhook URL 不允许包含嵌入凭据", status_code=422, code="INVALID_WEBHOOK_URL")

    explicitly_allowed_host = _host_matches_allowlist(host, env.webhook_allowed_hosts)
    if scheme == "https":
        pass
    elif scheme == "http" and (explicitly_allowed_host or env.webhook_allow_private_nets):
        pass
    else:
        raise AppError(
            "Webhook URL 必须使用 https://（内部 HTTP 接收方需由管理员在 WEBHOOK_ALLOWED_HOSTS 显式授权）",
            status_code=422,
            code="WEBHOOK_SCHEME_NOT_ALLOWED",
        )

    # 安全边界：外部项目 API Key 自定义订单 webhook_url 必须始终与管理员登记给该项目的默认回调同源，
    # 绝不允许借用全局 WEBHOOK_ALLOWED_HOSTS 越权回调其他内部租户或内部主机！
    if enforce_project_origin:
        if not allowed_project_url:
            raise AppError(
                "该项目未由管理员登记允许的 Webhook 目标，禁止自定义订单回调地址",
                status_code=403,
                code="WEBHOOK_TARGET_NOT_AUTHORIZED",
            )
        reg = urlparse(allowed_project_url.strip())
        reg_scheme = (reg.scheme or "").lower()
        reg_host = normalize_url_hostname(reg.hostname or "", error_code="INVALID_WEBHOOK_URL")
        reg_port = _safe_url_port(reg, error_code="INVALID_WEBHOOK_URL")
        eff_reg_port = reg_port or (443 if reg_scheme == "https" else 80)
        eff_parsed_port = parsed_port or (443 if scheme == "https" else 80)
        if (
            reg_scheme != scheme
            or reg_host != host
            or eff_reg_port != eff_parsed_port
        ):
            raise AppError(
                "订单 Webhook 地址必须与管理员登记的项目默认回调地址同源",
                status_code=403,
                code="WEBHOOK_TARGET_NOT_AUTHORIZED",
            )

    allow_private = explicitly_allowed_host or env.webhook_allow_private_nets
    allow_loopback = explicitly_allowed_host
    port = parsed_port or (443 if scheme == "https" else 80)

    literal_ip: Optional[str] = None
    try:
        ip_obj = ipaddress.ip_address(host)
        ip_text = str(ip_obj)
        if _is_forbidden_even_when_private_allowed(ip_text, allow_loopback=allow_loopback) or (
            not allow_private and _is_blocked_ip(ip_text)
        ):
            raise AppError(
                f"Webhook 目标地址 '{host}' 属于受限内网/回环/元数据网段，已被安全策略拦截",
                status_code=422,
                code="WEBHOOK_HOST_BLOCKED",
            )
        literal_ip = ip_text
    except ValueError:
        literal_ip = None

    return raw, host, port, allow_private, allow_loopback, literal_ip


def _check_resolved_ips(host: str, infos: list, allow_private: bool, *, allow_loopback: bool = False) -> list[str]:
    ips = list(dict.fromkeys(str(info[4][0]) for info in infos if info and info[4]))
    if not ips and not allow_private:
        raise AppError(f"Webhook 主机 '{host}' 无有效解析地址", status_code=422, code="WEBHOOK_DNS_FAILED")
    for ip_str in ips:
        if _is_forbidden_even_when_private_allowed(ip_str, allow_loopback=allow_loopback) or (
            not allow_private and _is_blocked_ip(ip_str)
        ):
            raise AppError(
                f"Webhook 主机 '{host}' 解析到受限地址 ({ip_str})，已被安全策略拦截",
                status_code=422,
                code="WEBHOOK_HOST_BLOCKED",
            )
    return ips


def validate_webhook_url(
    url: str,
    *,
    allowed_project_url: str = "",
    enforce_project_origin: bool = False,
    resolve_dns: bool = True,
) -> tuple[str, list[str]]:
    """同步校验 Webhook URL（供同步单元测试或无事件循环上下文使用）。"""
    raw, host, port, allow_private, allow_loopback, literal_ip = _validate_webhook_url_syntax(
        url,
        allowed_project_url=allowed_project_url,
        enforce_project_origin=enforce_project_origin,
    )
    if literal_ip is not None:
        return raw, [literal_ip]
    if not resolve_dns:
        return raw, []
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if not allow_private:
            raise AppError(
                f"Webhook 主机 '{host}' DNS 解析失败: {exc}",
                status_code=422,
                code="WEBHOOK_DNS_FAILED",
            ) from exc
        infos = []
    return raw, _check_resolved_ips(host, infos, allow_private, allow_loopback=allow_loopback)


async def validate_webhook_url_async(
    url: str,
    *,
    allowed_project_url: str = "",
    enforce_project_origin: bool = False,
    resolve_dns: bool = True,
    dns_timeout: float = 3.0,
) -> tuple[str, list[str]]:
    """异步校验 Webhook URL（使用带超时的异步 getaddrinfo，不阻塞事件循环，修复复核 P1-02）。"""
    raw, host, port, allow_private, allow_loopback, literal_ip = _validate_webhook_url_syntax(
        url,
        allowed_project_url=allowed_project_url,
        enforce_project_origin=enforce_project_origin,
    )
    if literal_ip is not None:
        return raw, [literal_ip]
    if not resolve_dns:
        return raw, []

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=max(0.2, float(dns_timeout)),
        )
    except asyncio.TimeoutError as exc:
        raise AppError(
            f"Webhook 主机 '{host}' DNS 解析超时 ({dns_timeout}s)",
            status_code=422,
            code="WEBHOOK_DNS_TIMEOUT",
            retryable=True,
        ) from exc
    except socket.gaierror as exc:
        if not allow_private:
            raise AppError(
                f"Webhook 主机 '{host}' DNS 解析失败: {exc}",
                status_code=422,
                code="WEBHOOK_DNS_FAILED",
            ) from exc
        infos = []
    return raw, _check_resolved_ips(host, infos, allow_private, allow_loopback=allow_loopback)


class PinnedIPNetworkBackend(httpcore.AsyncNetworkBackend):
    """强制将出站 TCP 连接绑定到已批准解析 IP 的底层网络后端（修复复核 P1-02）。

    httpcore 在 connect_tcp 返回流后仍使用原始域名作为 TLS server_hostname，
    因此既消除了 DNS Rebinding TOCTOU，又完整保留了原始域名的 TLS SNI 与证书校验。
    """

    def __init__(self, inner: Optional[httpcore.AsyncNetworkBackend] = None):
        self._inner = inner or AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            norm_host = normalize_url_hostname(host)
        except AppError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        route = _PINNED_WEBHOOK_ROUTE.get()

        if route is None or route.host != norm_host:
            # 未通过 pin_webhook_host 审批的连接一律禁止（除非是受允许的显式白名单字面量 IP）
            explicit_allow = _host_matches_allowlist(norm_host, env.webhook_allowed_hosts)
            allow_priv = explicit_allow or env.webhook_allow_private_nets
            try:
                ip_obj = ipaddress.ip_address(norm_host)
            except ValueError as exc:
                raise httpcore.ConnectError(f"Webhook 连接被拒：目标主机 '{norm_host}' 未绑定已批准 IP") from exc
            ip_text = str(ip_obj)
            if _is_forbidden_even_when_private_allowed(ip_text, allow_loopback=explicit_allow) or (
                not allow_priv and _is_blocked_ip(ip_text)
            ):
                raise httpcore.ConnectError(f"Webhook 连接被拒：目标 IP '{norm_host}' 属于受限网段")
            return await self._inner.connect_tcp(
                ip_text,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

        if not route.ips:
            if route.allow_private:
                return await self._inner.connect_tcp(
                    norm_host,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            raise httpcore.ConnectError(f"Webhook 连接被拒：主机 '{norm_host}' 无已批准的解析 IP")

        last_exc: Optional[Exception] = None
        for candidate_ip in route.ips:
            if _is_forbidden_even_when_private_allowed(candidate_ip, allow_loopback=route.allow_loopback) or (
                not route.allow_private and _is_blocked_ip(candidate_ip)
            ):
                raise httpcore.ConnectError(f"Webhook 连接被拒：已解析 IP '{candidate_ip}' 属于受限网段")
            try:
                return await self._inner.connect_tcp(
                    candidate_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Webhook 禁止连接本地 Unix Socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedIPAsyncTransport(httpx.AsyncHTTPTransport):
    """使用 PinnedIPNetworkBackend、禁用环境代理并关闭长连接复用的安全 Webhook 传输层。"""

    def __init__(
        self,
        *,
        verify: ssl.SSLContext | str | bool = True,
        network_backend: Optional[httpcore.AsyncNetworkBackend] = None,
    ):
        super().__init__(verify=verify, trust_env=False)
        ssl_context = httpx.create_ssl_context(verify=verify, trust_env=False)
        self.network_backend = network_backend or PinnedIPNetworkBackend()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=16,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            network_backend=self.network_backend,
        )


def validate_upstream_base_url(provider: str, url: str) -> str:
    """校验接码平台 Base URL，防止将平台 API Key 指向非授权域名或内网元数据服务。"""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    host = normalize_url_hostname(parsed.hostname or "", error_code="INVALID_UPSTREAM_URL")
    _safe_url_port(parsed, error_code="INVALID_UPSTREAM_URL")
    if not host or parsed.username or parsed.password:
        raise AppError("上游 Base URL 格式无效", status_code=422, code="INVALID_UPSTREAM_URL")

    allowed_defaults = DEFAULT_UPSTREAM_HOSTS.get(provider.lower(), frozenset())
    is_default_host = host in allowed_defaults
    is_custom_allowed = _host_matches_allowlist(host, env.allow_custom_upstream_hosts)

    if scheme != "https" and not is_custom_allowed:
        raise AppError(
            "上游平台 Base URL 必须使用 https://（自定义内网/镜像需在 ALLOW_CUSTOM_UPSTREAM_HOSTS 中显式放行）",
            status_code=422,
            code="UPSTREAM_URL_NOT_ALLOWED",
        )
    if not is_default_host and not is_custom_allowed:
        raise AppError(
            f"主机 '{host}' 不在 {provider} 默认域名白名单中；如需自定义代理域名，请在环境变量 ALLOW_CUSTOM_UPSTREAM_HOSTS 中配置",
            status_code=422,
            code="UPSTREAM_HOST_NOT_ALLOWED",
        )
    return raw


def validate_wa_app_url(url: str) -> str:
    """校验 wa-app 地址格式（仅管理员可修改）。"""
    raw = (url or "").strip().rstrip("/")
    parsed = urlparse(raw)
    _safe_url_port(parsed, error_code="INVALID_WA_URL")
    if (parsed.scheme or "").lower() not in {"http", "https"} or not parsed.hostname:
        raise AppError("wa-app 地址必须为有效的 http:// 或 https:// URL", status_code=422, code="INVALID_WA_URL")
    if parsed.username or parsed.password:
        raise AppError("wa-app 地址不允许内嵌凭据", status_code=422, code="INVALID_WA_URL")
    return raw
