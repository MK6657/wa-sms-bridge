"""配置管理：环境变量 + Fernet 加解密 + 安全边界开关。

默认以独立 SMS 接码中心模式运行（仅激活 SMSBower，不强依赖 wa-app）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


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
        return float(raw.strip())
    except ValueError:
        return default


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    items = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return list(dict.fromkeys(items)) or list(default)


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

        # ── 控制台管理员安全认证与对外 API 鉴权 ────────────────
        self.console_admin_password: str = os.environ.get("CONSOLE_ADMIN_PASSWORD", "").strip()
        self.console_admin_token: str = os.environ.get("CONSOLE_ADMIN_TOKEN", "").strip()
        self.admin_session_ttl_seconds: int = max(300, _env_int("ADMIN_SESSION_TTL_SECONDS", 86400))
        self.api_auth_required: bool = _env_bool("API_AUTH_REQUIRED", False)

        # ── 出站安全边界（SSRF 防护）─────────────────────────
        self.webhook_allowed_hosts: list[str] = _env_list("WEBHOOK_ALLOWED_HOSTS", ()) if os.environ.get("WEBHOOK_ALLOWED_HOSTS") else []
        self.webhook_allow_private_nets: bool = _env_bool("WEBHOOK_ALLOW_PRIVATE_NETS", False)
        self.allow_custom_upstream_hosts: list[str] = (
            _env_list("ALLOW_CUSTOM_UPSTREAM_HOSTS", ()) if os.environ.get("ALLOW_CUSTOM_UPSTREAM_HOSTS") else []
        )

        # ── 留存、Webhook 与缓存策略 ───────────────────────────
        self.sms_code_retention_hours: int = _env_int("SMS_CODE_RETENTION_HOURS", 24)
        self.sms_code_mask_in_history: bool = _env_bool("SMS_CODE_MASK_IN_HISTORY", True)
        self.webhook_timeout_seconds: float = max(1.0, _env_float("WEBHOOK_TIMEOUT_SECONDS", 8.0))
        self.country_cache_ttl_seconds: int = max(30, _env_int("COUNTRY_CACHE_TTL_SECONDS", 1800))
        self.price_cache_ttl_seconds: int = max(2, _env_int("PRICE_CACHE_TTL_SECONDS", 10))
        self.cors_origins: list[str] = _env_list("CORS_ORIGINS", ()) if os.environ.get("CORS_ORIGINS") else []

        # ── 可选插件与旧版 API 兼容（默认关闭）──────────────────
        self.enable_wa_plugin: bool = _env_bool("ENABLE_WA_PLUGIN", False)
        self.enable_legacy_api: bool = _env_bool("ENABLE_LEGACY_API", self.enable_wa_plugin)
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
        """用于签发 HttpOnly 管理员会话 Cookie 的对称密钥。"""
        if self._secret_key:
            return hashlib.sha256(f"sms-hub-session:{self._secret_key}".encode("utf-8")).digest()
        return self._ephemeral_session_secret

    def _derive_fernet_key(self, salt: bytes) -> bytes:
        """从 BRIDGE_SECRET_KEY 派生出 Fernet 兼容的 32 字节 key。"""
        raw = self._secret_key.encode("utf-8") if self._secret_key else b""
        if not raw:
            raise RuntimeError(
                "BRIDGE_SECRET_KEY 环境变量未设置！请设置一个固定值以确保加密密钥持久化。\n"
                "示例: set BRIDGE_SECRET_KEY=my-secure-key-2026"
            )
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
        return base64.urlsafe_b64encode(kdf.derive(raw))

    def encrypt_value(self, plaintext: str) -> str:
        """加密明文，返回 Fernet token 字符串。"""
        salt = os.urandom(16)
        key = self._derive_fernet_key(salt)
        f = Fernet(key)
        token = f.encrypt(plaintext.encode("utf-8"))
        return base64.urlsafe_b64encode(salt + token).decode("utf-8")

    def decrypt_value(self, encrypted: str) -> str:
        """解密 Fernet token，返回明文。"""
        raw = base64.urlsafe_b64decode(encrypted.encode("utf-8"))
        salt, token = raw[:16], raw[16:]
        key = self._derive_fernet_key(salt)
        f = Fernet(key)
        return f.decrypt(token).decode("utf-8")


env = Config()
