"""Cryptographic helpers for AgentForge signed requests and proof objects."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {char: index for index, char in enumerate(B58_ALPHABET)}
ED25519_MULTICODEC = b"\xed\x01"


def canonical_json(value: Any) -> str:
    """Return deterministic JSON suitable for hashing and signing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def base58btc_encode(raw: bytes) -> str:
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    number = int.from_bytes(raw, "big")
    if number == 0:
        return "1" * max(1, leading_zeroes)

    output = ""
    while number:
        number, remainder = divmod(number, 58)
        output = B58_ALPHABET[remainder] + output
    return "1" * leading_zeroes + output


def base58btc_decode(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            number = number * 58 + B58_INDEX[char]
        except KeyError as exc:
            raise ValueError("invalid base58btc character") from exc

    raw = b"" if number == 0 else number.to_bytes((number.bit_length() + 7) // 8, "big")
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + raw


def did_from_private_key(private_key: Ed25519PrivateKey) -> str:
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return "did:key:z" + base58btc_encode(ED25519_MULTICODEC + public_raw)


def public_key_from_did(did: str) -> Ed25519PublicKey:
    if not did.startswith("did:key:z"):
        raise ValueError("only did:key multibase identifiers are supported")
    encoded = did[len("did:key:z") :]
    decoded = base58btc_decode(encoded)
    if not decoded.startswith(ED25519_MULTICODEC) or len(decoded) != 34:
        raise ValueError("DID is not an Ed25519 did:key")
    return Ed25519PublicKey.from_public_bytes(decoded[2:])


def verify_signature(did: str, message: bytes, signature: str) -> bool:
    try:
        public_key_from_did(did).verify(b64url_decode(signature), message)
        return True
    except (ValueError, TypeError):
        return False
    except Exception:
        return False


def sign_bytes(private_key: Ed25519PrivateKey, message: bytes) -> str:
    return b64url_encode(private_key.sign(message))


def request_signing_bytes(
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    nonce: str,
) -> bytes:
    body_hash = sha256_bytes(body)
    canonical = "\n".join(
        [method.upper(), path, body_hash, timestamp, nonce]
    )
    return canonical.encode("utf-8")


def registration_signing_bytes(
    challenge_id: str,
    challenge_nonce: str,
    did: str,
    manifest: Any,
) -> bytes:
    manifest_hash = sha256_json(manifest)
    return "\n".join(
        ["REGISTER", challenge_id, challenge_nonce, did, manifest_hash]
    ).encode("utf-8")
