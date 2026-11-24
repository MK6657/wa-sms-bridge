"""配置管理：环境变量 + SQLite 配置表（含 Fernet 加密/解密 API Key）。"""

import os
import base64
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class Config:
    """环境变量配置（只读，启动时加载）。"""

    def __init__(self):
        self.wa_app_url: str = os.environ.get("WA_APP_URL", "http://host.docker.internal:18080").rstrip("/")
        self.wa_app_api_token: str = os.environ.get("WA_APP_API_TOKEN", "")
        self.wa_app_api_username: str = os.environ.get("WA_APP_API_USERNAME", "")
        self.wa_app_api_password: str = os.environ.get("WA_APP_API_PASSWORD", "")
        self.wa_app_auth_type: str = os.environ.get("WA_APP_AUTH_TYPE", "none")
        self.wa_adapter: str = os.environ.get("WA_ADAPTER", "whatsapp_wa")
        self.smsbower_base_url: str = os.environ.get("SMSBOWER_BASE_URL", "https://smsbower.page/stubs/handler_api.php").rstrip("/")
        self.smsbower_default_country: int = int(os.environ.get("SMSBOWER_DEFAULT_COUNTRY", "0"))
        self.bridge_data_dir: str = os.environ.get("BRIDGE_DATA_DIR", "/data")
        self._secret_key: str = os.environ.get("BRIDGE_SECRET_KEY", "")

    @property
    def db_path(self) -> str:
        return os.path.join(self.bridge_data_dir, "bridge.db")

    def _derive_fernet_key(self, salt: bytes) -> bytes:
        """从 BRIDGE_SECRET_KEY 派生出 Fernet 兼容的 32 字节 key。"""
        raw = self._secret_key.encode("utf-8") if self._secret_key else b""
        if not raw:
            raise RuntimeError(
                "BRIDGE_SECRET_KEY 环境变量未设置！请设置一个固定值以确保加密密钥持久化。\n"
                "示例: set BRIDGE_SECRET_KEY=my-secure-key-2026")
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
        return base64.urlsafe_b64encode(kdf.derive(raw))

    def encrypt_value(self, plaintext: str) -> str:
        """加密明文，返回 Fernet token 字符串。"""
        salt = os.urandom(16)
        key = self._derive_fernet_key(salt)
        f = Fernet(key)
        token = f.encrypt(plaintext.encode("utf-8"))
        # salt + token 一起存，解密时需要 salt
        return base64.urlsafe_b64encode(salt + token).decode("utf-8")

    def decrypt_value(self, encrypted: str) -> str:
        """解密 Fernet token，返回明文。"""
        raw = base64.urlsafe_b64decode(encrypted.encode("utf-8"))
        salt, token = raw[:16], raw[16:]
        key = self._derive_fernet_key(salt)
        f = Fernet(key)
        return f.decrypt(token).decode("utf-8")


env = Config()
