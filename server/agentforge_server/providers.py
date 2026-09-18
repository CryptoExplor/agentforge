"""Inference provider abstractions. Only mock/local execution is enabled in the MVP."""

from __future__ import annotations

import time
import threading
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .crypto import canonical_json, sha256_json
from .schemas import InferenceRequestCreate
from .services import new_id


class ProviderUnavailable(RuntimeError):
    pass


@dataclass
class InferenceSessionData:
    id: str
    provider: str
    status: str
    result: dict[str, Any]
    receipt: dict[str, Any]


class InferenceProvider(Protocol):
    async def quote(self, request: InferenceRequestCreate) -> dict[str, Any]: ...

    async def create(self, request: InferenceRequestCreate) -> InferenceSessionData: ...

    async def status(self, session_id: str) -> str: ...

    async def result(self, session_id: str) -> dict[str, Any]: ...

    async def receipt(self, session_id: str) -> dict[str, Any]: ...

    async def cancel(self, session_id: str) -> None: ...


class MockInferenceProvider:
    """Deterministic provider for local orchestration tests.

    It intentionally produces a clearly labelled result and receipt. It is not
    evidence of FLOP-network work.
    """

    # The API persists sessions in SQL. This is only a bounded convenience cache
    # for direct provider calls, not a second unbounded private-payload store.
    MAX_CACHED_SESSIONS = 64
    MAX_CACHED_BYTES = 4 * 1024 * 1024

    def __init__(self) -> None:
        self._sessions: OrderedDict[str, tuple[InferenceSessionData, int]] = OrderedDict()
        self._cached_bytes = 0
        self._cache_lock = threading.RLock()

    def _remember(self, data: InferenceSessionData) -> None:
        size = len(canonical_json({"result": data.result, "receipt": data.receipt}).encode("utf-8"))
        with self._cache_lock:
            if size > self.MAX_CACHED_BYTES:
                return
            while self._sessions and (len(self._sessions) >= self.MAX_CACHED_SESSIONS
                                      or self._cached_bytes + size > self.MAX_CACHED_BYTES):
                _, (_, removed_size) = self._sessions.popitem(last=False)
                self._cached_bytes -= removed_size
            self._sessions[data.id] = (data, size)
            self._cached_bytes += size

    def _cached(self, session_id: str) -> InferenceSessionData:
        with self._cache_lock:
            data, _ = self._sessions[session_id]  # Evicted/unknown direct calls raise KeyError.
            self._sessions.move_to_end(session_id)
            return data

    async def quote(self, request: InferenceRequestCreate) -> dict[str, Any]:
        return {
            "provider": "mock",
            "asset": "MOCK",
            "amount": "0",
            "estimated_latency_ms": min(request.max_latency_ms, 25),
            "requested_compute": request.requested_compute,
        }

    async def create(self, request: InferenceRequestCreate) -> InferenceSessionData:
        session_id = new_id("INF")
        requested = Decimal(request.requested_compute or "0")
        measured = requested if requested > 0 else Decimal("1000")
        result = {
            "provider": "mock",
            "model_ref": request.model_ref,
            "echo": request.input_data,
            "text": "MOCK inference result; not official FLOP network activity.",
        }
        result_hash = sha256_json(result)
        receipt = {
            "provider": "mock",
            "request_id": session_id,
            "model_ref": request.model_ref,
            "latency_ms": 1,
            "cost": {"amount": "0", "asset": "MOCK"},
            "requested_compute": str(requested),
            "measured_compute": str(measured),
            "paid_compute": str(measured),
            "verified_compute": str(measured),
            "result_hash": result_hash,
            "verification_status": "MOCK_VERIFIED",
            "official_network_receipt": None,
            "confidential": request.confidential,
            "created_at": time.time(),
        }
        data = InferenceSessionData(
            id=session_id,
            provider="mock",
            status="COMPLETED",
            result=result,
            receipt=receipt,
        )
        self._remember(data)
        return data

    async def status(self, session_id: str) -> str:
        return self._cached(session_id).status

    async def result(self, session_id: str) -> dict[str, Any]:
        return self._cached(session_id).result

    async def receipt(self, session_id: str) -> dict[str, Any]:
        return self._cached(session_id).receipt

    async def cancel(self, session_id: str) -> None:
        with self._cache_lock:
            cached = self._sessions.get(session_id)
            if cached and cached[0].status not in {"COMPLETED", "CANCELLED"}:
                cached[0].status = "CANCELLED"


MOCK_PROVIDER = MockInferenceProvider()


def get_provider(name: str) -> InferenceProvider:
    normalized = name.lower()
    if normalized in {"mock", "local"}:
        return MOCK_PROVIDER
    raise ProviderUnavailable(
        f"provider '{name}' is not enabled in the MVP; use mock/local"
    )
