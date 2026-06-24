"""Authenticated encryption helpers for support AI queue payloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from secret_config import env_secret


ENVELOPE_MARKER = "zendesk-support-ai-queue-v1"
ALG = "AES-256-GCM"


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _decode_key(raw: str) -> bytes:
    secret = raw.strip()
    if not secret:
        raise ValueError("SUPPORT_AI_QUEUE_KEY is empty")
    for candidate in (secret, secret.removeprefix("base64:")):
        try:
            decoded = base64.urlsafe_b64decode(candidate.encode("ascii"))
        except Exception:  # noqa: BLE001
            continue
        if len(decoded) in (16, 24, 32):
            return decoded
    return hashlib.sha256(secret.encode("utf-8")).digest()


def load_key() -> Optional[bytes]:
    """Return the queue encryption key, or None when encryption is not configured."""
    raw = env_secret("SUPPORT_AI_QUEUE_KEY")
    if not raw:
        return None
    return _decode_key(raw)


def encrypt_json(data: dict[str, Any], *, key: bytes) -> dict[str, Any]:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    aad = ENVELOPE_MARKER.encode("ascii")
    ciphertext = AESGCM(key).encrypt(nonce, body, aad)
    return {
        "_encrypted": True,
        "format": ENVELOPE_MARKER,
        "alg": ALG,
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ciphertext),
    }


def is_encrypted_json(data: Any) -> bool:
    return isinstance(data, dict) and data.get("_encrypted") is True and data.get("format") == ENVELOPE_MARKER


def decrypt_json(envelope: dict[str, Any], *, key: bytes) -> dict[str, Any]:
    if envelope.get("alg") != ALG:
        raise ValueError(f"unsupported queue encryption alg: {envelope.get('alg')}")
    nonce = _b64d(str(envelope["nonce"]))
    ciphertext = _b64d(str(envelope["ciphertext"]))
    aad = ENVELOPE_MARKER.encode("ascii")
    body = AESGCM(key).decrypt(nonce, ciphertext, aad)
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("decrypted spool JSON is not an object")
    return decoded
