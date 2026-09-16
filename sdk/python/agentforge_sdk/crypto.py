from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ED25519_MULTICODEC = b"\xed\x01"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def b58encode(raw: bytes) -> str:
    zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    number = int.from_bytes(raw, "big")
    if not number:
        return "1" * max(1, zeroes)
    result = ""
    while number:
        number, remainder = divmod(number, 58)
        result = B58_ALPHABET[remainder] + result
    return "1" * zeroes + result


def did_from_private_key(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "did:key:z" + b58encode(ED25519_MULTICODEC + raw)


def request_bytes(method: str, path: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), path, body_hash, timestamp, nonce]).encode()


def registration_bytes(challenge_id: str, challenge_nonce: str, did: str, manifest: Any) -> bytes:
    return "\n".join(
        ["REGISTER", challenge_id, challenge_nonce, did, sha256_json(manifest)]
    ).encode()
