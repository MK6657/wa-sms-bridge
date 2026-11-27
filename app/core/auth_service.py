"""管理员会话鉴权与项目级 API Key 鉴权服务。

安全边界（修复 P0-01 / P1-08 / P1-10）：
1. 彻底禁止依赖客户端可伪造的 X-SMS-Hub-Client / Origin / Referer 标头授予管理员权限。
2. 管理员身份仅由以下可验证方式之一产生：
   - 合法签名的 HttpOnly Cookie (`sms_hub_session`)，由 `/api/v1/auth/login`（配置口令时）
     或本机直连回环 `/api/v1/auth/session`（未设口令且无任何代理转发头时）签发；
   - 配置了 `CONSOLE_ADMIN_TOKEN` 时请求头中的匹配令牌；
   - 仅在本机直连回环、未配置口令、未启用严格鉴权且无任何反代转发头时允许本机直接调试访问。
3. 浏览器原生 `EventSource`（SSE）自动携带 HttpOnly Cookie，严格模式下工作台 SSE 同样安全通过认证。
4. 项目 API Key 只能访问自身项目的订单/统计/私有黑名单，严格隔离管理员接口与跨项目数据。
"""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import Request, Response

from app.config import env
from app.db import Database
from app.errors import AppError, AuthError, ConflictError, ForbiddenError

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "sms_hub_session"
CSRF_HEADER_NAME = "x-sms-hub-csrf"
DEFAULT_SCOPES = ["orders:rw", "prices:r", "blacklist:rw", "events:r"]
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ASGI_TEST_HOSTS = frozenset({"testserver", "test"})
PROXY_FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-server",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "true-client-ip",
    "via",
    "cdn-loop",
)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _slugify_project_id(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (name or "").strip().lower()).strip("_-")
    if not cleaned:
        cleaned = f"proj_{secrets.token_hex(4)}"
    return cleaned[:48]


def _sign_session_token(expires_at: int, *, epoch: int = 1) -> str:
    nonce = secrets.token_hex(8)
    payload = f"admin:{expires_at}:{epoch}:{nonce}"
    sig = hmac.new(env.session_signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: str, *, current_epoch: int = 1) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(env.session_signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    parts = payload.split(":")
    if len(parts) == 4 and parts[0] == "admin":
        try:
            expires_at = int(parts[1])
            token_epoch = int(parts[2])
        except ValueError:
            return False
        if token_epoch != current_epoch:
            return False
        return time.time() < expires_at
    if len(parts) == 3 and parts[0] == "admin":
        if current_epoch != 1:
            return False
        try:
            expires_at = int(parts[1])
        except ValueError:
            return False
        return time.time() < expires_at
    return False


def _is_loopback_ip_literal(host_str: str) -> bool:
    candidate = (host_str or "").strip().lower()
    if not candidate:
        return False
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback)


def _extract_host_without_port(raw_host: str) -> str:
    text = (raw_host or "").strip().lower()
    if not text:
        return ""
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end != -1 else text
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text


def _canonicalize_origin(origin_or_url: str) -> str:
    raw = (origin_or_url or "").strip()
    if not raw or raw.lower() == "null":
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    host_fmt = f"[{hostname}]" if ":" in hostname else hostname
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host_fmt}"
    return f"{scheme}://{host_fmt}:{port}"


def _ip_in_trusted_proxies(host_str: str) -> bool:
    cand = (host_str or "").strip().lower()
    if not cand or not env.trusted_proxy_ips:
        return False
    if cand.startswith("[") and cand.endswith("]"):
        cand = cand[1:-1]
    if "%" in cand:
        cand = cand.split("%", 1)[0]
    try:
        ip_obj = ipaddress.ip_address(cand)
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            ip_obj = ip_obj.ipv4_mapped
    except ValueError:
        return cand in env.trusted_proxy_ips
    for rule in env.trusted_proxy_ips:
        r = rule.strip()
        if not r:
            continue
        try:
            if "/" in r:
                net = ipaddress.ip_network(r, strict=False)
                if ip_obj in net:
                    return True
            else:
                target_ip = ipaddress.ip_address(r)
                if isinstance(target_ip, ipaddress.IPv6Address) and target_ip.ipv4_mapped is not None:
                    target_ip = target_ip.ipv4_mapped
                if ip_obj == target_ip:
                    return True
        except ValueError:
            if cand == r.lower():
                return True
    return False


def _is_trusted_proxy_client(request: Request) -> bool:
    client_host = (getattr(request.client, "host", "") or "").strip()
    return _ip_in_trusted_proxies(client_host)


def _allowed_browser_origins(request: Request) -> set[str]:
    allowed: set[str] = set()
    from_trusted_proxy = _is_trusted_proxy_client(request)
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() if from_trusted_proxy else ""
    scheme = fwd_proto if fwd_proto in {"http", "https"} else (request.url.scheme or "http").lower()
    host_hdr = (
        (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        if from_trusted_proxy
        else (request.headers.get("host") or "").split(",")[0].strip()
    )
    if host_hdr:
        same_origin = _canonicalize_origin(f"{scheme}://{host_hdr}")
        if same_origin:
            allowed.add(same_origin)
    req_origin = _canonicalize_origin(f"{request.url.scheme}://{request.url.netloc}")
    if req_origin:
        allowed.add(req_origin)
    for cfg_origin in env.admin_trusted_origins:
        if cfg_origin and cfg_origin != "*":
            canon = _canonicalize_origin(cfg_origin)
            if canon:
                allowed.add(canon)
    return allowed


def enforce_browser_csrf(request: Request) -> None:
    """对基于 Cookie 或本机免密回环的写操作执行浏览器同源 / Fetch-Metadata CSRF 防护。"""
    if request.method.upper() not in UNSAFE_HTTP_METHODS:
        return

    allowed_origins = _allowed_browser_origins(request)
    explicit_admin_origins = {
        _canonicalize_origin(o)
        for o in env.admin_trusted_origins
        if o and o != "*" and _canonicalize_origin(o)
    }

    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    raw_origin = (request.headers.get("origin") or "").strip()
    canon_origin = _canonicalize_origin(raw_origin) if raw_origin else ""

    if sec_fetch_site and sec_fetch_site not in {"same-origin", "none"}:
        if not (canon_origin and canon_origin in explicit_admin_origins):
            raise ForbiddenError("跨站写请求已被 CSRF 安全策略拦截", code="CSRF_REJECTED")

    if raw_origin:
        if not canon_origin or canon_origin not in allowed_origins:
            raise ForbiddenError("请求 Origin 与服务不同源，已被 CSRF 安全策略拦截", code="CSRF_REJECTED")
    else:
        raw_referer = (request.headers.get("referer") or "").strip()
        if raw_referer:
            canon_referer = _canonicalize_origin(raw_referer)
            if not canon_referer or canon_referer not in allowed_origins:
                raise ForbiddenError("请求 Referer 与服务不同源，已被 CSRF 安全策略拦截", code="CSRF_REJECTED")

    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type in {"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"}:
        if (request.headers.get(CSRF_HEADER_NAME) or "").strip() != "1":
            raise ForbiddenError("表单类型写请求缺少 X-SMS-Hub-CSRF 防护头", code="CSRF_REJECTED")


def is_direct_loopback(request: Request) -> bool:
    """仅当直连本机回环、Host 为本机回环主机名、非跨站请求且不存在反代头时返回 True（防 DNS Rebinding）。"""
    for header_name in PROXY_FORWARD_HEADERS:
        if request.headers.get(header_name):
            return False
    client_host = (getattr(request.client, "host", "") or "").strip().lower()
    is_asgi_test = client_host == "testclient"
    if not is_asgi_test and not _is_loopback_ip_literal(client_host):
        return False

    host_only = _extract_host_without_port(request.headers.get("host") or "")
    if host_only:
        allowed_hosts = ALLOWED_LOOPBACK_HOSTS | ASGI_TEST_HOSTS
        if host_only not in allowed_hosts and not _is_loopback_ip_literal(host_only):
            return False

    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_fetch_site and sec_fetch_site not in {"same-origin", "none"}:
        return False

    allowed_origins = _allowed_browser_origins(request)
    raw_origin = (request.headers.get("origin") or "").strip()
    if raw_origin:
        canon_origin = _canonicalize_origin(raw_origin)
        if not canon_origin or canon_origin not in allowed_origins:
            return False
    else:
        raw_referer = (request.headers.get("referer") or "").strip()
        if raw_referer:
            canon_referer = _canonicalize_origin(raw_referer)
            if not canon_referer or canon_referer not in allowed_origins:
                return False

    return True


@dataclass(frozen=True)
class CallerContext:
    project_id: str
    name: str
    authenticated: bool
    is_admin: bool = False
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    default_webhook_url: str = ""
    webhook_secret: str = ""
    key_hash: str = ""
    key_version: int = 0

    @property
    def is_console(self) -> bool:
        """向后兼容别名：等同于 is_admin。"""
        return self.is_admin

    def has_scope(self, scope: str) -> bool:
        if self.is_admin:
            return True
        if scope in self.scopes:
            return True
        if ":" in scope:
            prefix, action = scope.split(":", 1)
            if action in {"r", "w"} and f"{prefix}:rw" in self.scopes:
                return True
        if "*" in self.scopes:
            # 非管理员项目密钥的通配符仅覆盖租户级接码权限，绝不隐式授予全局黑名单或下层插件权限
            if scope == "blacklist:global" or scope.startswith("wa:"):
                return False
            return True
        return False


class AuthService:
    """统一管理会话与项目 API Key 鉴权。"""

    _LOGIN_WINDOW_SECONDS = 60.0
    _LOGIN_MAX_FAILURES = 10
    _GLOBAL_MAX_FAILURES = 40
    _EPOCH_CACHE_TTL_SECONDS = 2.0
    _TOUCH_INTERVAL_SECONDS = 60.0

    def __init__(self, db: Database):
        self._db = db
        self._bg_tasks: set[asyncio.Task] = set()
        self._session_epoch: Optional[int] = None
        self._session_epoch_fetched_at: float = 0.0
        self._failed_logins: dict[str, deque[float]] = {}
        self._global_failed_logins: deque[float] = deque()
        self._last_touch_mono: dict[str, float] = {}

    async def _get_session_epoch(self) -> int:
        now = time.monotonic()
        if self._session_epoch is None or (now - self._session_epoch_fetched_at) >= self._EPOCH_CACHE_TTL_SECONDS:
            raw = await self._db.config.get("admin_session_epoch", "1")
            try:
                self._session_epoch = max(1, int(str(raw).strip()))
            except ValueError:
                self._session_epoch = 1
            self._session_epoch_fetched_at = now
        return self._session_epoch

    async def is_auth_required(self) -> bool:
        raw = await self._db.config.get("api_auth_required", "")
        if raw != "":
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(env.api_auth_required)

    async def set_auth_required(self, required: bool) -> None:
        await self._db.config.set("api_auth_required", "true" if required else "false")

    def password_configured(self) -> bool:
        return bool(env.console_admin_password)

    def _client_rate_limit_key(self, request: Request) -> str:
        peer_host = (getattr(request.client, "host", "") or "unknown").strip()
        if _ip_in_trusted_proxies(peer_host):
            xff_parts = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
            for candidate in reversed(xff_parts):
                if not _ip_in_trusted_proxies(candidate):
                    return candidate[:64]
            real_ip = (request.headers.get("x-real-ip") or "").strip()
            if real_ip:
                return real_ip[:64]
        return peer_host[:64]

    def _prune_login_buckets(self, now: float) -> None:
        while self._global_failed_logins and now - self._global_failed_logins[0] > self._LOGIN_WINDOW_SECONDS:
            self._global_failed_logins.popleft()
        expired_keys: list[str] = []
        for k, b in self._failed_logins.items():
            while b and now - b[0] > self._LOGIN_WINDOW_SECONDS:
                b.popleft()
            if not b:
                expired_keys.append(k)
        for k in expired_keys:
            self._failed_logins.pop(k, None)

    def check_login_rate_limit(self, request: Request) -> None:
        key = self._client_rate_limit_key(request)
        now = time.monotonic()
        while self._global_failed_logins and now - self._global_failed_logins[0] > self._LOGIN_WINDOW_SECONDS:
            self._global_failed_logins.popleft()
        if len(self._global_failed_logins) >= self._GLOBAL_MAX_FAILURES:
            raise AppError(
                "当前登录失败请求过于频繁，请 1 分钟后再试",
                status_code=429,
                code="AUTH_RATE_LIMITED",
                retryable=True,
            )
        bucket = self._failed_logins.get(key)
        if not bucket:
            return
        while bucket and now - bucket[0] > self._LOGIN_WINDOW_SECONDS:
            bucket.popleft()
        if not bucket:
            self._failed_logins.pop(key, None)
            return
        if len(bucket) >= self._LOGIN_MAX_FAILURES:
            raise AppError(
                "管理员口令错误次数过多，请 1 分钟后再试",
                status_code=429,
                code="AUTH_RATE_LIMITED",
                retryable=True,
            )

    def record_login_result(self, request: Request, *, success: bool) -> None:
        key = self._client_rate_limit_key(request)
        if success:
            self._failed_logins.pop(key, None)
            return
        now = time.monotonic()
        self._global_failed_logins.append(now)
        if len(self._failed_logins) >= 1024 and key not in self._failed_logins:
            self._prune_login_buckets(now)
            if len(self._failed_logins) >= 1024:
                oldest_key = next(iter(self._failed_logins))
                self._failed_logins.pop(oldest_key, None)
        bucket = self._failed_logins.setdefault(key, deque())
        while bucket and now - bucket[0] > self._LOGIN_WINDOW_SECONDS:
            bucket.popleft()
        bucket.append(now)
        safe_key = re.sub(r"[\r\n\t\x00-\x1f\x7f]", "_", key)[:64]
        logger.warning("管理员控制台登录失败 source=%s failures_in_window=%d", safe_key, len(bucket))

    async def issue_admin_cookie(self, response: Response, *, request: Request) -> int:
        ttl = env.admin_session_ttl_seconds
        expires_at = int(time.time()) + ttl
        epoch = await self._get_session_epoch()
        token = _sign_session_token(expires_at, epoch=epoch)
        fwd_proto = (
            (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if _is_trusted_proxy_client(request)
            else ""
        )
        is_https = (
            (request.url.scheme or "").lower() == "https"
            or env.admin_cookie_secure
            or (self.password_configured() and fwd_proto == "https")
        )
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=ttl,
            httponly=True,
            samesite="lax",
            secure=is_https,
            path="/",
        )
        return expires_at

    async def revoke_and_clear_admin_cookie(self, request: Request, response: Response) -> None:
        cookie_token = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
        if cookie_token:
            epoch = await self._get_session_epoch()
            if _verify_session_token(cookie_token, current_epoch=epoch):
                await self._db.conn.write(
                    "INSERT INTO config (key, value, updated_at) VALUES ('admin_session_epoch', '2', datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = CAST(MAX(1, CAST(COALESCE(NULLIF(value, ''), '1') AS INTEGER)) + 1 AS TEXT), "
                    "updated_at = datetime('now')"
                )
                self._session_epoch = None
                self._session_epoch_fetched_at = 0.0
                await self._get_session_epoch()
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")

    def clear_admin_cookie(self, response: Response) -> None:
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")

    def verify_admin_password(self, password: str) -> bool:
        """使用 UTF-8 字节做常量时间比较，完整支持中文及任意 Unicode 管理员口令（修复补充 P2-01）。"""
        if not env.console_admin_password:
            return False
        actual = (password or "").encode("utf-8")
        expected = env.console_admin_password.encode("utf-8")
        return hmac.compare_digest(actual, expected)

    async def create_project_key(
        self,
        *,
        name: str,
        project_id: str = "",
        scopes: Optional[list[str]] = None,
        default_webhook_url: str = "",
        webhook_secret: str = "",
    ) -> tuple[dict[str, Any], str]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("项目名称不能为空")
        pid = _slugify_project_id(project_id or clean_name)
        if pid in {"web-console", "admin", "global"}:
            raise ConflictError(f"项目 ID '{pid}' 为系统保留名称", code="RESERVED_PROJECT_ID")

        # 补充 P1-03: 检查永久项目注册表（含已删除历史项目），绝不允许新建项目复用历史 project_id
        if await self._db.api_keys.project_exists(pid):
            if project_id:
                raise ConflictError(
                    f"项目 ID '{pid}' 已存在或曾被历史项目使用；新建项目不可复用旧身份，如需更换密钥请调用轮转接口",
                    code="PROJECT_EXISTS",
                )
            base_pid = pid[:40]
            while True:
                candidate = f"{base_pid}_{secrets.token_hex(3)}"
                if not await self._db.api_keys.project_exists(candidate):
                    pid = candidate
                    break

        raw_key = f"sms_k_{secrets.token_urlsafe(24)}"
        key_prefix = raw_key[:12]
        key_hash = hash_api_key(raw_key)
        # 复核 P1-06: 明确区分省略 scopes (None -> DEFAULT_SCOPES) 与显式传空列表 ([] -> 零权限)
        if scopes is None:
            final_scopes = list(DEFAULT_SCOPES)
        else:
            final_scopes = [s.strip() for s in scopes if str(s).strip()]

        record = await self._db.api_keys.create(
            project_id=pid,
            name=clean_name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=final_scopes,
            default_webhook_url=default_webhook_url,
            webhook_secret=webhook_secret,
        )
        return record, raw_key

    async def rotate_project_key(self, project_id: str) -> Optional[tuple[dict[str, Any], str]]:
        """在现有项目身份内原子轮转密钥：新密钥继承历史数据访问权，旧密钥立即失效（修复补充 P1-03）。"""
        raw_key = f"sms_k_{secrets.token_urlsafe(24)}"
        key_prefix = raw_key[:12]
        key_hash = hash_api_key(raw_key)
        updated = await self._db.api_keys.rotate_key(
            project_id,
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
        if updated is None:
            return None
        return updated, raw_key

    async def verify_active_credential(self, caller: CallerContext, *, required_scope: str = "") -> bool:
        """复核长连接（如 SSE）当前持有的项目凭据是否仍有效、未被停用/轮转/删除且仍具备所需权限（修复补充 P1-03）。"""
        if caller.is_admin:
            return True
        if not caller.project_id or not caller.key_hash:
            return False
        record = await self._db.api_keys.get(caller.project_id, include_hash=True)
        if record is None or not record.get("enabled"):
            return False
        if not hmac.compare_digest(
            str(record.get("key_hash") or "").encode("utf-8"),
            caller.key_hash.encode("utf-8"),
        ):
            return False
        if caller.key_version and int(record.get("key_version") or 0) != caller.key_version:
            return False
        if required_scope:
            live_scopes = list(record.get("scopes") or [])
            live_ctx = CallerContext(
                project_id=caller.project_id,
                name=caller.name,
                authenticated=True,
                is_admin=False,
                scopes=live_scopes,
            )
            if not live_ctx.has_scope(required_scope):
                return False
        return True

    async def verify_stream_request(
        self,
        request: Request,
        caller: CallerContext,
        *,
        required_scope: str = "events:r",
    ) -> bool:
        """周期复核长连接（SSE）会话与凭据，覆盖项目密钥轮转/停用、管理员 Cookie 过期及开启严格鉴权。"""
        if not caller.is_admin:
            return await self.verify_active_credential(caller, required_scope=required_scope)
        raw_key, from_query = self._extract_raw_key(request)
        if raw_key and not from_query and env.console_admin_token:
            return hmac.compare_digest(
                raw_key.encode("utf-8"),
                env.console_admin_token.encode("utf-8"),
            )
        cookie_token = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
        if cookie_token:
            epoch = await self._get_session_epoch()
            return _verify_session_token(cookie_token, current_epoch=epoch)
        if env.console_admin_password or await self.is_auth_required():
            return False
        return is_direct_loopback(request)

    def _extract_raw_key(self, request: Request) -> tuple[str, bool]:
        header_key = (request.headers.get("X-API-Key") or "").strip()
        if header_key:
            return header_key, False
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip(), False
        # 仅对 SSE 流允许 query api_key（给无 Cookie 的外部项目 EventSource，严禁承载管理员 Token）
        if request.url.path.endswith("/events/stream"):
            query_key = (request.query_params.get("api_key") or "").strip()
            if query_key:
                return query_key, True
        return "", False

    async def authenticate_request(self, request: Request) -> CallerContext:
        # 1. 显式凭据优先：管理员 Token（仅限请求头）或项目 API Key
        raw_key, from_query = self._extract_raw_key(request)
        if raw_key:
            if env.console_admin_token and hmac.compare_digest(
                raw_key.encode("utf-8"),
                env.console_admin_token.encode("utf-8"),
            ):
                if from_query:
                    raise AuthError(
                        "禁止在 URL 查询参数中传递管理员令牌，请改用请求头或管理员会话 Cookie",
                        code="AUTH_ADMIN_TOKEN_IN_QUERY_FORBIDDEN",
                    )
                return CallerContext(
                    project_id="web-console",
                    name="管理员令牌",
                    authenticated=True,
                    is_admin=True,
                    scopes=["*"],
                )

            key_digest = hash_api_key(raw_key)
            record = await self._db.api_keys.get_by_hash(key_digest)
            if record is None:
                raise AuthError("无效的 API Key", code="AUTH_INVALID_KEY")
            if not record.get("enabled"):
                raise AuthError("该 API Key 已被停用", code="AUTH_KEY_DISABLED")
            pid_touch = str(record["project_id"])
            now_mono = time.monotonic()
            if (now_mono - self._last_touch_mono.get(pid_touch, 0.0)) >= self._TOUCH_INTERVAL_SECONDS:
                if len(self._last_touch_mono) >= 1024 and pid_touch not in self._last_touch_mono:
                    self._last_touch_mono.pop(next(iter(self._last_touch_mono)), None)
                self._last_touch_mono[pid_touch] = now_mono
                touch_task = asyncio.create_task(self._safe_touch(pid_touch))
                self._bg_tasks.add(touch_task)
                touch_task.add_done_callback(self._bg_tasks.discard)
            # 复核 P1-06: 保留数据库中的精确权限列表，绝不在清空权限([])后回退到 DEFAULT_SCOPES
            raw_scopes = record.get("scopes")
            resolved_scopes = list(raw_scopes) if isinstance(raw_scopes, list) else []
            return CallerContext(
                project_id=record["project_id"],
                name=record["name"],
                authenticated=True,
                is_admin=False,
                scopes=resolved_scopes,
                default_webhook_url=record.get("default_webhook_url") or "",
                webhook_secret=record.get("webhook_secret") or "",
                key_hash=key_digest,
                key_version=int(record.get("key_version") or 1),
            )

        # 2. 服务端签名 HttpOnly 管理员会话 Cookie（同时覆盖 REST 与浏览器 SSE）
        cookie_token = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
        if cookie_token:
            epoch = await self._get_session_epoch()
            if _verify_session_token(cookie_token, current_epoch=epoch):
                enforce_browser_csrf(request)
                return CallerContext(
                    project_id="web-console",
                    name="控制台管理员",
                    authenticated=True,
                    is_admin=True,
                    scopes=["*"],
                )
            raise AuthError("管理员会话已失效，请重新登录", code="AUTH_SESSION_EXPIRED")

        # 3. 无凭据请求：严禁信任 X-SMS-Hub-Client / Origin / Referer
        #    若设置了管理员口令、处于严格模式或非本机直连回环（含经过任何反代头），一律拒绝！
        if env.console_admin_password:
            raise AuthError("请先登录管理员控制台或提供有效的项目 API Key", code="AUTH_REQUIRED")

        if await self.is_auth_required():
            raise AuthError(
                "已启用严格鉴权模式，请提供管理员会话、X-API-Key 或 Authorization: Bearer 令牌",
                code="AUTH_REQUIRED",
            )

        if not is_direct_loopback(request):
            raise AuthError(
                "非本机直连请求必须提供有效凭据（请配置 CONSOLE_ADMIN_PASSWORD 或使用项目 API Key）",
                code="AUTH_REMOTE_DENIED",
            )

        enforce_browser_csrf(request)

        # 仅限单机直连回环、未设口令且未开启严格模式时的开发免密模式
        return CallerContext(
            project_id="web-console",
            name="本机直连管理员",
            authenticated=False,
            is_admin=True,
            scopes=["*"],
        )

    async def _safe_touch(self, project_id: str) -> None:
        try:
            await self._db.api_keys.touch_last_used(project_id)
        except Exception:
            pass

    @staticmethod
    def ensure_admin(caller: CallerContext, message: str = "该操作仅限管理员执行") -> None:
        if not caller.is_admin:
            raise ForbiddenError(message)

    @staticmethod
    def ensure_scope(caller: CallerContext, scope: str) -> None:
        if not caller.has_scope(scope):
            raise ForbiddenError(f"密钥缺少所需权限: {scope}")
