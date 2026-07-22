"""OCR 数据库凭证加密。"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_CIPHERTEXT_PREFIX = "fernet:v1:"


def encrypt_ocr_credential(value: str) -> str:
    """使用部署级共享密钥加密数据库凭证。"""

    token = _cipher().encrypt(value.encode()).decode()
    return f"{_CIPHERTEXT_PREFIX}{token}"


def decrypt_ocr_credential(value: str | None) -> str | None:
    """解密数据库凭证，并在密钥不匹配时显式失败。"""

    if not value:
        return None
    if not value.startswith(_CIPHERTEXT_PREFIX):
        raise ValueError("OCR 数据库凭证不是受支持的加密格式，请重新保存密钥")
    try:
        return _cipher().decrypt(value.removeprefix(_CIPHERTEXT_PREFIX).encode()).decode()
    except InvalidToken as exc:
        raise ValueError("OCR 数据库凭证无法解密，请检查凭证加密密钥是否发生变化") from exc


def _cipher() -> Fernet:
    """从显式配置的部署密钥派生 OCR 专用 Fernet 密钥。"""

    # API 与 worker 必须使用同一持久密钥，否则数据库凭证无法跨进程或重启解密。
    encryption_secret = (
        os.getenv("OCR_CREDENTIAL_ENCRYPTION_KEY", "").strip() or os.getenv("JWT_SECRET_KEY", "").strip()
    )
    if not encryption_secret:
        raise ValueError("数据库密钥模式需要配置持久化的 OCR_CREDENTIAL_ENCRYPTION_KEY 或 JWT_SECRET_KEY")
    derived_key = hashlib.sha256(f"yuxi:ocr:{encryption_secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))
