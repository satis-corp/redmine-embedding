"""Encryption of stored secrets and hashing of the tokens we issue.

Upstream (Redmine) tokens and DCR client records are stored encrypted (Fernet:
AES-128-CBC + HMAC-SHA256, from ``cryptography``). The tokens / codes / state
values *we* issue are random 256-bit strings and are stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import OAuthConfigError


class DecryptError(Exception):
    """A stored value cannot be decrypted (key changed / data corrupted). No value in the message."""


class TokenCipher:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])
        except (ValueError, TypeError) as e:  # never echo the key
            raise OAuthConfigError("MCP_OAUTH_ENCRYPTION_KEY が不正です（Fernet 鍵: url-safe base64 の 32 バイト）") from e
        if not parts:
            raise OAuthConfigError("MCP_OAUTH_ENCRYPTION_KEY が空です")

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise DecryptError("stored value could not be decrypted") from None


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
