"""Authenticated encryption helpers for Knowledge API fields."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENVELOPE_MARKER = "knowledge-api-field-v1"
ALG = "AES-256-GCM"


def env_secret(name: str, default: str = "") -> str:
    file_name = os.environ.get(f"{name}_FILE", "")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _decode_key(raw: str) -> bytes:
    secret = raw.strip()
    if not secret:
        raise ValueError("KNOWLEDGE_FIELD_KEY is empty")
    for candidate in (secret, secret.removeprefix("base64:")):
        try:
            decoded = base64.urlsafe_b64decode(candidate.encode("ascii"))
        except Exception:  # noqa: BLE001
            continue
        if len(decoded) in (16, 24, 32):
            return decoded
    return hashlib.sha256(secret.encode("utf-8")).digest()


def load_key() -> bytes:
    raw = env_secret("KNOWLEDGE_FIELD_KEY")
    if not raw:
        raise RuntimeError("KNOWLEDGE_FIELD_KEY is not configured")
    return _decode_key(raw)


def encrypt_text(value: str, *, key: Optional[bytes] = None) -> str:
    key = key or load_key()
    nonce = os.urandom(12)
    aad = ENVELOPE_MARKER.encode("ascii")
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)
    envelope = {
        "format": ENVELOPE_MARKER,
        "alg": ALG,
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ciphertext),
    }
    return json.dumps(envelope, separators=(",", ":"))


def decrypt_text(value: str, *, key: Optional[bytes] = None) -> str:
    if not value:
        return ""
    key = key or load_key()
    envelope: dict[str, Any] = json.loads(value)
    if envelope.get("format") != ENVELOPE_MARKER:
        raise ValueError("unsupported Knowledge API field encryption format")
    if envelope.get("alg") != ALG:
        raise ValueError(f"unsupported Knowledge API field encryption alg: {envelope.get('alg')}")
    nonce = _b64d(str(envelope["nonce"]))
    ciphertext = _b64d(str(envelope["ciphertext"]))
    aad = ENVELOPE_MARKER.encode("ascii")
    return AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8")


def hmac_text(value: str, *, key: Optional[bytes] = None) -> str:
    key = key or load_key()
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
