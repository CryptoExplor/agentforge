"""Minimal dependency-light AgentForge client/worker SDK."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import (
    canonical_json,
    did_from_private_key,
    registration_bytes,
    request_bytes,
    sha256_json,
)


class AgentForgeError(RuntimeError):
    pass


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
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"did": self.did, "private_key_hex": self.private_key_hex}, indent=2),
            encoding="utf-8",
        )
        try:
            target.chmod(0o600)
        except OSError:
            pass

    @property
    def key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key_hex))

    @property
    def did(self) -> str:
        return did_from_private_key(self.key)

    def sign(self, message: bytes) -> str:
        import base64

        return base64.urlsafe_b64encode(self.key.sign(message)).decode().rstrip("=")


class AgentForgeClient:
    def __init__(
        self,
        base_url: str,
        identity: AgentIdentity,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "AgentForgeClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise AgentForgeError(f"HTTP {response.status_code}: {detail}")
        return response.json()

    def _signed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        signing_path: str | None = None,
    ) -> Any:
        body = b"" if payload is None else canonical_json(payload).encode()
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signature = self.identity.sign(
            request_bytes(method, signing_path or path, body, timestamp, nonce)
        )
        headers = {
            "Content-Type": "application/json",
            "X-Agent-DID": self.identity.did,
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Nonce": nonce,
            "X-Agent-Signature": signature,
            "Idempotency-Key": nonce,
        }
        response = self.http.request(method, self.base_url + path, content=body, headers=headers)
        return self._decode(response)

    def register(self, manifest: dict[str, Any]) -> dict[str, Any]:
        challenge = self._decode(self.http.get(self.base_url + "/api/v1/register/challenge"))
        payload = {
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "did": self.identity.did,
            "manifest": manifest,
        }
        signature = self.identity.sign(
            registration_bytes(
                challenge["challenge_id"],
                challenge["nonce"],
                self.identity.did,
                manifest,
            )
        )
        payload["signature"] = signature
        response = self.http.post(
            self.base_url + "/api/v1/agents/register",
            content=canonical_json(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self._decode(response)

    def get_agent(self, did: str | None = None) -> dict[str, Any]:
        return self._decode(self.http.get(self.base_url + "/api/v1/agents/" + (did or self.identity.did)))

    def balance(self, asset: str = "MOCK") -> dict[str, Any]:
        path = f"/api/v1/agents/{self.identity.did}/balance?asset={asset}"
        return self._signed_request(
            "GET",
            path,
            {},
            signing_path=f"/api/v1/agents/{self.identity.did}/balance",
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._signed_request("GET", f"/api/v1/tasks/{task_id}", {})

    def list_tasks(self, **filters: Any) -> dict[str, Any]:
        params = {key: value for key, value in filters.items() if value is not None}
        return self._decode(self.http.get(self.base_url + "/api/v1/tasks", params=params))

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return self._signed_request("POST", "/api/v1/tasks", task)

    def claim(self, task_id: str) -> dict[str, Any]:
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/claim", {})

    def heartbeat(self, claim_id: str) -> dict[str, Any]:
        return self._signed_request("POST", f"/api/v1/claims/{claim_id}/heartbeat", {})

    def infer(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/inference", request)

    def submit(
        self,
        task_id: str,
        *,
        result: dict[str, Any],
        evidence: list[dict[str, Any]] | None = None,
        inference_session_ids: list[str] | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        submission_id = submission_id or f"S_{uuid.uuid4().hex}"
        task = self._signed_request("GET", f"/api/v1/tasks/{task_id}", {})
        created_at = time.time()
        proof_core = {
            "task_id": task_id,
            "submission_id": submission_id,
            "executor_did": self.identity.did,
            "input_hash": sha256_json(task.get("input", {})),
            "result_hash": sha256_json(result),
            "evidence": evidence or [],
            "inference_session_ids": inference_session_ids or [],
            "created_at": created_at,
        }
        payload = {
            "submission_id": submission_id,
            "result": result,
            "evidence": evidence or [],
            "inference_session_ids": inference_session_ids or [],
            "created_at": created_at,
            "proof_signature": self.identity.sign(canonical_json(proof_core).encode()),
        }
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/submissions", payload)

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        return self._signed_request("GET", f"/api/v1/submissions/{submission_id}", {})

    def get_proof(self, submission_id: str) -> dict[str, Any]:
        return self._signed_request("GET", f"/api/v1/proofs/{submission_id}", {})

    def open_dispute(
        self,
        submission_id: str,
        *,
        reason: str,
        additional_evidence: list[dict[str, Any]] | None = None,
        dispute_id: str | None = None,
    ) -> dict[str, Any]:
        return self._signed_request(
            "POST",
            f"/api/v1/submissions/{submission_id}/disputes",
            {
                "dispute_id": dispute_id or f"D_{uuid.uuid4().hex}",
                "reason": reason,
                "additional_evidence": additional_evidence or [],
            },
        )

    def resolve_dispute(
        self,
        dispute_id: str,
        *,
        submission_id: str,
        decision: str,
        checks: list[dict[str, Any]] | None = None,
        reason_codes: list[str] | None = None,
        settlement: dict[str, Any] | None = None,
        policy: str = "deterministic_then_domain",
        decision_id: str | None = None,
        evidence_hash: str | None = None,
    ) -> dict[str, Any]:
        decision_id = decision_id or f"VD_{uuid.uuid4().hex}"
        evidence_hash = evidence_hash or self.get_submission(submission_id)["proof_hash"]
        core = {
            "decision_id": decision_id,
            "submission_id": submission_id,
            "validator_did": self.identity.did,
            "decision": decision,
            "policy": policy,
            "checks": checks or [],
            "reason_codes": reason_codes or [],
            "settlement": settlement or {},
            "evidence_hash": evidence_hash or "",
        }
        payload = {key: value for key, value in core.items() if key not in {"submission_id", "validator_did"}}
        payload["signature"] = self.identity.sign(canonical_json(core).encode())
        return self._signed_request("POST", f"/api/v1/disputes/{dispute_id}/resolve", payload)

    def validate(
        self,
        submission_id: str,
        *,
        decision: str,
        checks: list[dict[str, Any]] | None = None,
        reason_codes: list[str] | None = None,
        settlement: dict[str, Any] | None = None,
        policy: str = "deterministic_then_domain",
        decision_id: str | None = None,
        evidence_hash: str | None = None,
    ) -> dict[str, Any]:
        submission = self._signed_request("GET", f"/api/v1/submissions/{submission_id}", {})
        decision_id = decision_id or f"VD_{uuid.uuid4().hex}"
        core = {
            "decision_id": decision_id,
            "submission_id": submission_id,
            "validator_did": self.identity.did,
            "decision": decision,
            "policy": policy,
            "checks": checks or [],
            "reason_codes": reason_codes or [],
            "settlement": settlement or {},
            "evidence_hash": evidence_hash or submission["proof_hash"],
        }
        payload = {key: value for key, value in core.items() if key not in {"submission_id", "validator_did"}}
        payload["signature"] = self.identity.sign(canonical_json(core).encode())
        return self._signed_request("POST", f"/api/v1/submissions/{submission_id}/validate", payload)

    def events(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        path = f"/api/v1/events?limit={limit}"
        if cursor:
            from urllib.parse import quote

            path += "&cursor=" + quote(cursor, safe="")
        return self._signed_request("GET", path, {}, signing_path="/api/v1/events")

    def reputation(self, did: str | None = None) -> dict[str, Any]:
        target = did or self.identity.did
        return self._decode(self.http.get(self.base_url + f"/api/v1/reputation/{target}"))
