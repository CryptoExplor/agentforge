"""Agent identity: Ed25519 key generation, persistence and raw signing.

``AgentIdentity`` is the client-side trust root: it derives the agent's
``did:key`` DID from its Ed25519 private key, signs protocol messages
(requests, registration, proofs, validation decisions) and persists itself
to disk atomically and private from the first write. The key format, DID
derivation and signing bytes are unchanged from the original monolithic
``client.py``; only the module location moved.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import did_from_private_key
from .errors import AgentForgeError


@dataclass
class AgentIdentity:
    private_key_hex: str

    @classmethod
    def generate(cls) -> "AgentIdentity":
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return cls(raw.hex())

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "AgentIdentity":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        identity = cls(data["private_key_hex"])
        if identity.did != data.get("did"):
            raise AgentForgeError("identity file DID does not match its private key")
        return identity

    def save(self, path: str | os.PathLike[str]) -> None:
        """Atomically replace an identity file, private from its first write.

        The caller must use a trusted directory. Never write through a target
        symlink or expose a newly created key before a later chmod. Filesystem
        failures propagate; the previous identity survives a failed replacement.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"did": self.did, "private_key_hex": self.private_key_hex}, indent=2)
        fd, temporary = tempfile.mkstemp(prefix=".agentforge-identity-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @property
    def key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key_hex))

    @property
    def did(self) -> str:
        return did_from_private_key(self.key)

    def sign(self, message: bytes) -> str:
        return base64.urlsafe_b64encode(self.key.sign(message)).decode().rstrip("=")
