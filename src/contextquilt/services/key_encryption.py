"""
Symmetric encryption for storing third-party API keys at rest.
Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.

The encryption key is derived from CQ_KEY_ENCRYPTION_KEY env var.
If not set, keys are stored as plaintext (local dev only).
"""

import os
import base64
import hashlib
from typing import Optional

_ENCRYPTION_KEY = os.getenv("CQ_KEY_ENCRYPTION_KEY", "")


def _get_fernet():
    """Get a Fernet instance from the env var, or None if not configured."""
    if not _ENCRYPTION_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        # Derive a 32-byte key from the env var via SHA-256, base64-encode for Fernet
        key_bytes = hashlib.sha256(_ENCRYPTION_KEY.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        return Fernet(fernet_key)
    except ImportError:
        return None


def encrypt_key(plaintext: str) -> str:
    """Encrypt an API key for storage. Returns ciphertext string."""
    f = _get_fernet()
    if f:
        return f.encrypt(plaintext.encode()).decode()
    # Fallback: store plaintext (local dev only)
    return plaintext


def decrypt_key(ciphertext: str) -> str:
    """Decrypt an API key from storage. Returns plaintext string."""
    f = _get_fernet()
    if f:
        try:
            return f.decrypt(ciphertext.encode()).decode()
        except Exception:
            # If decryption fails (key rotated, corrupted), return empty
            return ""
    # Fallback: assume plaintext
    return ciphertext


def mask_key(key: str) -> str:
    """Mask an API key for display: sk-or-...xxxx"""
    if not key or len(key) < 8:
        return "***"
    return key[:6] + "..." + key[-4:]
