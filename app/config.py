"""配置管理：环境变量 + Fernet 加解密 + 安全边界开关。

默认以独立 SMS 接码中心模式运行（仅激活 SMSBower，不强依赖 wa-app）。
"""

from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import hmac
import math
import os
import secrets
from urllib.parse import urlparse
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

APP_VERSION = "1.2.0"
_V2_CIPHERTEXT_PREFIX = b"v2"
_MASTER_KDF_SALT = b"sms-hub-master-kdf-v2\x00"


@lru_cache(maxsize=4096)
def _derive_legacy_pbkdf2_fernet_key(secret_bytes: bytes, salt: bytes) -> bytes:
    """缓存旧版 v1 密文的 100k PBKDF2 派生结果，避免重复解密阻塞事件循环。"""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
    return base64.urlsafe_b64encode(kdf.derive(secret_bytes))


@lru_cache(maxsize=64)
def _derive_master_key(secret_bytes: bytes) -> bytes:
    """每个主密钥仅执行一次 100k PBKDF2 强化，后续每条记录通过高速 HKDF 派生独立子密钥。"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_MASTER_KDF_SALT,
        iterations=100_000,
    )
    return kdf.derive(secret_bytes)


def _derive_v2_fernet_key(secret_bytes: bytes, salt: bytes) -> bytes:
    master_key = _derive_master_key(secret_bytes)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"sms-hub-fernet-v2",
    )
    return base64.urlsafe_b64encode(hkdf.derive(master_key))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
        return val if math.isfinite(val) else default
    except ValueError:
        return default


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    items = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return list(dict.fromkeys(items)) or list(default)


def _env_origins(name: str) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return []
    origins: list[str] = []
    for part in raw.split(","):
        item = part.strip().rstrip("/")
        if not item:
            continue
        if item == "*":
            normalized = "*"
        else:
            parsed = urlparse(item)
            if parsed.scheme and parsed.netloc:
                normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            else:
                normalized = item
        if normalized not in origins:
            origins.append(normalized)
    return origins


class Config:
    """环境变量配置（启动时加载，可通过 reload() 刷新）。"""

    def __init__(self):
        self._ephemeral_session_secret: bytes = secrets.token_bytes(32)
        self.reload()

    def reload(self):
        # ── SMS Hub 核心配置 ───────────────────────────────────
        self.active_providers: list[str] = _env_list("ACTIVE_PROVIDERS", ("smsbower",))
        raw_default = os.environ.get("DEFAULT_PROVIDER", "").strip().lower()
        self.default_provider: str = raw_default or self.active_providers[0]
        if self.default_provider not in self.active_providers:
            self.active_providers.append(self.default_provider)

        self.smsbower_base_url: str = os.environ.get(
            "SMSBOWER_BASE_URL",
            "https://smsbower.page/stubs/handler_api.php",
        ).rstrip("/")
        self.smsbower_default_country: int = _env_int("SMSBOWER_DEFAULT_COUNTRY", 52)
        self.bridge_data_dir: str = os.environ.get("BRIDGE_DATA_DIR", "/data")
        self._secret_key: str = os.environ.get("BRIDGE_SECRET_KEY", "")
        raw_prev_keys = os.environ.get("BRIDGE_SECRET_KEY_PREVIOUS", "")
        self._previous_secret_keys: list[str] = [
            k.strip() for k in raw_prev_keys.split(",") if k.strip() and k.strip() != self._secret_key
        ]

        # ── 控制台管理员安全认证与对外 API 鉴权 ────────────────
        self.console_admin_password: str = os.environ.get("CONSOLE_ADMIN_PASSWORD", "").strip()
        self.console_admin_token: str = os.environ.get("CONSOLE_ADMIN_TOKEN", "").strip()
        self.admin_session_ttl_seconds: int = max(300, _env_int("ADMIN_SESSION_TTL_SECONDS", 86400))
        self.admin_cookie_secure: bool = _env_bool("ADMIN_COOKIE_SECURE", False)
        self.api_auth_required: bool = _env_bool("API_AUTH_REQUIRED", False)
        self.trusted_proxy_ips: list[str] = (
            _env_list("TRUSTED_PROXY_IPS", ()) if os.environ.get("TRUSTED_PROXY_IPS") else []
        )
        self.admin_trusted_origins: list[str] = _env_origins("ADMIN_TRUSTED_ORIGINS")

        # ── 出站安全边界（SSRF 防护）─────────────────────────
        self.webhook_allowed_hosts: list[str] = _env_list("WEBHOOK_ALLOWED_HOSTS", ()) if os.environ.get("WEBHOOK_ALLOWED_HOSTS") else []
        self.webhook_allow_private_nets: bool = _env_bool("WEBHOOK_ALLOW_PRIVATE_NETS", False)
        self.allow_custom_upstream_hosts: list[str] = (
            _env_list("ALLOW_CUSTOM_UPSTREAM_HOSTS", ()) if os.environ.get("ALLOW_CUSTOM_UPSTREAM_HOSTS") else []
        )

        # ── 留存、Webhook 与缓存策略 ───────────────────────────
        self.sms_code_retention_hours: int = _env_int("SMS_CODE_RETENTION_HOURS", 24)
        self.sms_code_mask_in_history: bool = _env_bool("SMS_CODE_MASK_IN_HISTORY", True)
        self.webhook_timeout_seconds: float = min(30.0, max(1.0, _env_float("WEBHOOK_TIMEOUT_SECONDS", 8.0)))
        self.country_cache_ttl_seconds: int = max(30, _env_int("COUNTRY_CACHE_TTL_SECONDS", 1800))
        self.price_cache_ttl_seconds: int = max(2, _env_int("PRICE_CACHE_TTL_SECONDS", 10))
        self.cors_origins: list[str] = _env_origins("CORS_ORIGINS")

        # ── 可选插件与旧版 API 兼容（默认关闭）──────────────────
        self.enable_wa_plugin: bool = _env_bool("ENABLE_WA_PLUGIN", False)
        self.enable_legacy_api: bool = _env_bool("ENABLE_LEGACY_API", False)
        self.wa_app_url: str = os.environ.get("WA_APP_URL", "http://host.docker.internal:18080").rstrip("/")
        self.wa_app_api_token: str = os.environ.get("WA_APP_API_TOKEN", "")
        self.wa_app_api_username: str = os.environ.get("WA_APP_API_USERNAME", "")
        self.wa_app_api_password: str = os.environ.get("WA_APP_API_PASSWORD", "")
        self.wa_app_auth_type: str = os.environ.get("WA_APP_AUTH_TYPE", "none")
        self.wa_adapter: str = os.environ.get("WA_ADAPTER", "whatsapp_wa")

    @property
    def db_path(self) -> str:
        return os.path.join(self.bridge_data_dir, "bridge.db")

    @property
    def has_secret_key(self) -> bool:
        return bool(self._secret_key)

    @property
    def session_signing_key(self) -> bytes:
        """用于签发 HttpOnly 管理员会话 Cookie 的对称密钥（绑定管理员口令摘要，改密即失效）。"""
        pw_digest = hashlib.sha256(self.console_admin_password.encode("utf-8")).hexdigest()
        base = self._secret_key.encode("utf-8") if self._secret_key else self._ephemeral_session_secret
        return hmac.new(base, f"sms-hub-session:{pw_digest}".encode("utf-8"), hashlib.sha256).digest()

    def hmac_digest(self, namespace: str, value: str) -> str:
        """生成不可逆带密钥 HMAC-SHA256 摘要（用于脱敏后重发防重比对，不落盘明文）。"""
        base = self._secret_key.encode("utf-8") if self._secret_key else self._ephemeral_session_secret
        return hmac.new(base, f"{namespace}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_hmac_digest(self, namespace: str, value: str, expected_hex: str) -> bool:
        """校验带密钥 HMAC-SHA256 摘要，兼容 BRIDGE_SECRET_KEY_PREVIOUS 平滑轮换。"""
        if not expected_hex:
            return False
        msg = f"{namespace}:{value}".encode("utf-8")
        if self._secret_key:
            bases = [self._secret_key.encode("utf-8")]
            for prev in self._previous_secret_keys:
                enc = prev.encode("utf-8")
                if enc and enc not in bases:
                    bases.append(enc)
        else:
            bases = [self._ephemeral_session_secret]
        return any(
            hmac.compare_digest(hmac.new(base, msg, hashlib.sha256).hexdigest(), expected_hex)
            for base in bases
        )

    def _require_secret_bytes(self) -> bytes:
        raw = self._secret_key.encode("utf-8") if self._secret_key else b""
        if not raw:
            raise RuntimeError(
                "BRIDGE_SECRET_KEY 环境变量未设置！请设置一个固定值以确保加密密钥持久化。\n"
                "示例: set BRIDGE_SECRET_KEY=my-secure-key-2026"
            )
        return raw

    def _candidate_secret_bytes(self) -> list[bytes]:
        primary = self._require_secret_bytes()
        candidates = [primary]
        for prev in self._previous_secret_keys:
            enc = prev.encode("utf-8")
            if enc and enc not in candidates:
                candidates.append(enc)
        return candidates

    def _derive_fernet_key(self, salt: bytes) -> bytes:
        """兼容保留：从当前主密钥按旧版 v1 PBKDF2 派生 Fernet key（带内存缓存）。"""
        return _derive_legacy_pbkdf2_fernet_key(self._require_secret_bytes(), salt)

    def encrypt_value(self, plaintext: str) -> str:
        """加密明文（v2 主密钥单次 PBKDF2 + 每记录随机盐 HKDF），返回 URL-safe base64 密文。"""
        secret_bytes = self._require_secret_bytes()
        salt = os.urandom(16)
        key = _derive_v2_fernet_key(secret_bytes, salt)
        f = Fernet(key)
        token = f.encrypt(plaintext.encode("utf-8"))
        return base64.urlsafe_b64encode(_V2_CIPHERTEXT_PREFIX + salt + token).decode("utf-8")

    def decrypt_value(self, encrypted: str) -> str:
        """解密 v2 或旧版 v1 Fernet 密文，支持 BRIDGE_SECRET_KEY_PREVIOUS 平滑轮换。"""
        try:
            raw = base64.urlsafe_b64decode(encrypted.encode("utf-8"))
        except Exception as exc:
            raise InvalidToken("Invalid base64 ciphertext") from exc
        candidates = self._candidate_secret_bytes()
        last_exc: Exception | None = None

        # 优先按 v2 ("v2" + 16B salt + Fernet token "gAAAA...") 解密；失败即报错，绝不回落触发 100k PBKDF2！
        if (
            raw.startswith(_V2_CIPHERTEXT_PREFIX)
            and len(raw) > 24
            and raw[18:23] == b"gAAAA"
        ):
            v2_salt, v2_token = raw[2:18], raw[18:]
            for secret_bytes in candidates:
                try:
                    key = _derive_v2_fernet_key(secret_bytes, v2_salt)
                    return Fernet(key).decrypt(v2_token).decode("utf-8")
                except InvalidToken as exc:
                    last_exc = exc
            raise last_exc or InvalidToken("Invalid v2 ciphertext")

        # 回退兼容旧版 v1 (16B salt + Fernet token "gAAAA...")，校验 Fernet 头后再执行缓存 PBKDF2
        if len(raw) > 24 and raw[16:21] == b"gAAAA":
            v1_salt, v1_token = raw[:16], raw[16:]
            for secret_bytes in candidates:
                try:
                    key = _derive_legacy_pbkdf2_fernet_key(secret_bytes, v1_salt)
                    return Fernet(key).decrypt(v1_token).decode("utf-8")
                except InvalidToken as exc:
                    last_exc = exc

        if last_exc is not None:
            raise last_exc
        raise InvalidToken("Invalid ciphertext payload")


env = Config()
