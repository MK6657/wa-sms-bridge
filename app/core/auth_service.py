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
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import Request, Response

from app.config import env
from app.db import Database
from app.errors import AuthError, ConflictError, ForbiddenError

SESSION_COOKIE_NAME = "sms_hub_session"
DEFAULT_SCOPES = ["orders:rw", "prices:r", "blacklist:rw", "events:r"]
PROXY_FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "true-client-ip",
)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _slugify_project_id(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (name or "").strip().lower()).strip("_-")
    if not cleaned:
        cleaned = f"proj_{secrets.token_hex(4)}"
    return cleaned[:48]


def _sign_session_token(expires_at: int) -> str:
    nonce = secrets.token_hex(8)
    payload = f"admin:{expires_at}:{nonce}"
    sig = hmac.new(env.session_signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(env.session_signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "admin":
        return False
    try:
        expires_at = int(parts[1])
    except ValueError:
        return False
    return time.time() < expires_at


def is_direct_loopback(request: Request) -> bool:
    """仅当直连本机回环且不存在任何反向代理转发头时返回 True。"""
    for header_name in PROXY_FORWARD_HEADERS:
        if request.headers.get(header_name):
            return False
    client_host = (getattr(request.client, "host", "") or "").strip().lower()
    return client_host in {"127.0.0.1", "::1", "testclient"}


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
        if self.is_admin or "*" in self.scopes:
            return True
        if scope in self.scopes:
            return True
        if ":" in scope:
            prefix, action = scope.split(":", 1)
            if action in {"r", "w"} and f"{prefix}:rw" in self.scopes:
                return True
        return False


class AuthService:
    """统一管理会话与项目 API Key 鉴权。"""

    def __init__(self, db: Database):
        self._db = db
        self._bg_tasks: set[asyncio.Task] = set()

    async def is_auth_required(self) -> bool:
        raw = await self._db.config.get("api_auth_required", "")
        if raw != "":
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(env.api_auth_required)

    async def set_auth_required(self, required: bool) -> None:
        await self._db.config.set("api_auth_required", "true" if required else "false")

    def password_configured(self) -> bool:
        return bool(env.console_admin_password)

    def issue_admin_cookie(self, response: Response, *, request: Request) -> int:
        ttl = env.admin_session_ttl_seconds
        expires_at = int(time.time()) + ttl
        token = _sign_session_token(expires_at)
        is_https = (request.url.scheme or "").lower() == "https"
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
            return _verify_session_token(cookie_token)
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
            touch_task = asyncio.create_task(self._safe_touch(record["project_id"]))
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
            if _verify_session_token(cookie_token):
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
