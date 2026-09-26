"""AgentForge SDK client facade.

``AgentForgeClient`` remains the single public entry point of the SDK. The
implementation lives in focused modules — :mod:`agentforge_sdk.identity`
(key generation, atomic persistence, DID derivation, raw signing),
:mod:`agentforge_sdk.errors` (the error hierarchy),
:mod:`agentforge_sdk.transport` (signed HTTP, ``X-Server-Timestamp`` drift
calibration, error mapping) and :mod:`agentforge_sdk.crypto` (canonical
signing bytes) — and this module composes them, so both legacy import paths
``from agentforge_sdk import ...`` and
``from agentforge_sdk.client import ...`` keep working unchanged.

The flat method surface is the compatibility contract: every historical
method keeps its name, signature and response shape, and new endpoints are
added as further flat methods rather than a nested namespace.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import quote

# ``os`` and ``tempfile`` are deliberately still imported here. The identity
# implementation moved to ``agentforge_sdk.identity``, but these module
# attributes remain a supported patch surface on the facade (the security
# suite injects filesystem failures through them, and they are the same
# module objects the identity code calls), so removing them would break
# callers that reached the same objects through this module.
import os  # noqa: F401  (compatibility patch surface, see above)
import tempfile  # noqa: F401  (compatibility patch surface, see above)

from .crypto import canonical_json, registration_bytes, sha256_json
from .errors import AgentForgeError
from .identity import AgentIdentity
from .transport import Transport

__all__ = ["AgentForgeClient", "AgentIdentity", "AgentForgeError"]


class AgentForgeClient:
    """Signed client for the AgentForge HTTP API."""

    def __init__(
        self,
        base_url: str,
        identity: AgentIdentity,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.identity = identity
        self.transport = Transport(self.base_url, timeout=timeout)

    # ------------------------------------------------------------------
    # Transport surface, kept as client attributes for compatibility
    # ------------------------------------------------------------------

    @property
    def http(self) -> Any:
        """The underlying HTTP client (injectable, e.g. an ASGI test client)."""
        return self.transport.http

    @http.setter
    def http(self, value: Any) -> None:
        self.transport.http = value

    @property
    def clock_offset(self) -> float:
        """Seconds added to the local clock when signing (server-calibrated)."""
        return self.transport.clock_offset

    @clock_offset.setter
    def clock_offset(self, value: float) -> None:
        self.transport.clock_offset = value

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "AgentForgeClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _decode(self, response: Any) -> Any:
        """Compatibility delegate to :meth:`Transport.decode`."""
        return self.transport.decode(response)

    def _sync_clock(self, response: Any) -> None:
        """Compatibility delegate to :meth:`Transport.sync_clock`."""
        self.transport.sync_clock(response)

    def _signed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        signing_path: str | None = None,
    ) -> Any:
        """Compatibility delegate to :meth:`Transport.signed_request`."""
        return self.transport.signed_request(
            self.identity, method, path, payload, signing_path=signing_path
        )

    # ------------------------------------------------------------------
    # Agents: registration, profile, discovery, capability index
    # ------------------------------------------------------------------

    def register(self, manifest: dict[str, Any]) -> dict[str, Any]:
        challenge = self.transport.decode(self.transport.get("/api/v1/register/challenge"))
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
        response = self.transport.post(
            "/api/v1/agents/register",
            content=canonical_json(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self.transport.decode(response)

    def get_agent(self, did: str | None = None) -> dict[str, Any]:
        return self.transport.decode(
            self.transport.get("/api/v1/agents/" + (did or self.identity.did))
        )

    def capabilities(self) -> dict[str, Any]:
        """Capability index: declared capability names with agent counts."""
        return self.transport.decode(self.transport.get("/api/v1/capabilities"))

    def search_agents(
        self,
        *,
        capability: str | None = None,
        chain: str | None = None,
        min_reputation: float | None = None,
    ) -> dict[str, Any]:
        """Search active agents.

        ``capability`` and ``chain`` are server-side filters evaluated by the
        API. ``min_reputation`` is applied locally to the
        ``reputation.overall`` field of the returned agent views: the API does
        not expose a reputation filter, so the SDK filters what the server
        already returned (bounded by the server's own result limits) instead
        of inventing a query parameter.
        """
        params = {
            key: value
            for key, value in {"capability": capability, "chain": chain}.items()
            if value is not None
        }
        result = self.transport.decode(self.transport.get("/api/v1/agents/search", params=params))
        if min_reputation is None:
            return result
        agents = [
            agent
            for agent in result.get("agents", [])
            if (agent.get("reputation") or {}).get("overall", 0.0) >= min_reputation
        ]
        return {**result, "agents": agents}

    def balance(self, asset: str = "MOCK") -> dict[str, Any]:
        path = f"/api/v1/agents/{self.identity.did}/balance?asset={asset}"
        return self._signed_request(
            "GET",
            path,
            {},
            signing_path=f"/api/v1/agents/{self.identity.did}/balance",
        )

    def reputation(self, did: str | None = None) -> dict[str, Any]:
        target = did or self.identity.did
        return self.transport.decode(self.transport.get(f"/api/v1/reputation/{target}"))

    # ------------------------------------------------------------------
    # Tasks: creation, discovery, cancellation
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._signed_request("GET", f"/api/v1/tasks/{task_id}", {})

    def list_tasks(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        verification_strategy: str | None = None,
        capability: str | None = None,
        chain: str | None = None,
        origin: str | None = None,
        min_reward: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        offset: int | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """List public tasks with the server's SQL-backed filters.

        Backward compatible: the historical keyword-only filter calls
        (``status``, ``kind``, ``verification_strategy``, ``capability``,
        ``chain``, ``origin``, ``min_reward``, ``limit`` and any other query
        parameter passed through ``**filters``) keep the exact legacy
        ``{"tasks": [...]}`` response.

        Passing the opaque ``cursor`` from a previous page's ``next_cursor``
        (keyset seek — stable under concurrent inserts) or a plain ``offset``
        opts into pagination metadata: the server then also returns
        ``total``, ``limit``, ``offset``, ``has_more`` and ``next_cursor``.
        When both are supplied, the cursor wins.
        """
        params = {
            key: value
            for key, value in {
                "status": status,
                "kind": kind,
                "verification_strategy": verification_strategy,
                "capability": capability,
                "chain": chain,
                "origin": origin,
                "min_reward": min_reward,
                "limit": limit,
                "cursor": cursor,
                "offset": offset,
                **filters,
            }.items()
            if value is not None
        }
        return self.transport.decode(self.transport.get("/api/v1/tasks", params=params))

    def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return self._signed_request("POST", "/api/v1/tasks", task)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel one of the caller's own open/funded tasks.

        Only the requester may cancel; escrow is refunded and the task becomes
        ``CANCELLED``. A claimed or already-settled task returns a 409
        conflict instead of being force-cancelled.
        """
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/cancel", {})

    # ------------------------------------------------------------------
    # Claims and inference
    # ------------------------------------------------------------------

    def claim(self, task_id: str) -> dict[str, Any]:
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/claim", {})

    def heartbeat(self, claim_id: str) -> dict[str, Any]:
        return self._signed_request("POST", f"/api/v1/claims/{claim_id}/heartbeat", {})

    def infer(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/inference", request)

    def get_inference_session(self, session_id: str) -> dict[str, Any]:
        """Retrieve a persisted inference session by its ID.

        The request is signed because a session on a non-public task
        authorizes the read through the same signed-request headers; for
        public tasks the extra headers are simply not required.
        """
        return self._signed_request("GET", f"/api/v1/inference/{session_id}", {})

    # ------------------------------------------------------------------
    # Submissions, proofs and validation
    # ------------------------------------------------------------------

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

    def validate_task(
        self,
        task_id: str,
        *,
        decision: str,
        submission_id: str | None = None,
        evidence_hash: str | None = None,
        checks: list[dict[str, Any]] | None = None,
        reason_codes: list[str] | None = None,
        settlement: dict[str, Any] | None = None,
        policy: str = "deterministic_then_domain",
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a signed peer-validation decision scoped to a task.

        ``POST /api/v1/tasks/{task_id}/validations`` lets the server resolve
        the task's submission that is awaiting validation (``SUBMITTED``, or
        ``DISPUTED`` so the validation also closes the dispute). The decision
        signature nevertheless covers that submission's ID and its proof
        hash, so the validator must already know both: pass ``submission_id``
        (its proof hash is fetched to build the signature when
        ``evidence_hash`` is not supplied) or supply ``evidence_hash``
        directly alongside ``submission_id``.
        """
        if submission_id is None:
            raise ValueError(
                "validate_task() requires submission_id: the validation decision "
                "signature covers the task's pending submission id and proof hash, "
                "which the server resolves from the task but the signer must "
                "already know"
            )
        if evidence_hash is None:
            evidence_hash = self.get_submission(submission_id)["proof_hash"]
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
            "evidence_hash": evidence_hash or "",
        }
        payload = {key: value for key, value in core.items() if key not in {"submission_id", "validator_did"}}
        payload["signature"] = self.identity.sign(canonical_json(core).encode())
        return self._signed_request("POST", f"/api/v1/tasks/{task_id}/validations", payload)

    # ------------------------------------------------------------------
    # Disputes
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def events(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        path = f"/api/v1/events?limit={limit}"
        if cursor:
            path += "&cursor=" + quote(cursor, safe="")
        return self._signed_request("GET", path, {}, signing_path="/api/v1/events")
