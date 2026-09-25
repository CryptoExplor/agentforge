"""Bound ingress before JSON parsing/signature verification and preserve raw bytes."""
from __future__ import annotations

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import clock
from .admission import AdmissionDenied, consume_ingress
from .settings import settings

MAX_BODY_BYTES = 2_000_000


class RequestSecurityMiddleware:
    def __init__(self, app):
        self.app = app
        self._limiter = anyio.CapacityLimiter(settings.max_inflight_requests)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        try:
            self._limiter.acquire_nowait()
        except anyio.WouldBlock:
            response = JSONResponse({"detail": "request concurrency limit exceeded"}, status_code=429,
                                    headers={"Retry-After": "1", "Cache-Control": "no-store"})
            return await response(scope, receive, send)
        try:
            await self._handle_http(scope, receive, send)
        finally:
            self._limiter.release()

    async def _handle_http(self, scope, receive, send):
        # Authoritative receipt time, stamped before the body is read so every
        # downstream lease, expiry and deadline is anchored to the moment the
        # server took responsibility for the request (Grok roadmap 1.4). A
        # client clock is never consulted here or anywhere else. Endpoints read
        # it back through request.state.received_at.
        clock.stamp_scope(scope)

        async def reject(status, detail, headers=None):
            response = JSONResponse({"detail": detail}, status_code=status, headers={
                "Cache-Control": "no-store", **(headers or {}),
            })
            await response(scope, receive, send)

        if len(scope.get("query_string", b"")) > 8192 or len(scope["path"].encode()) > 2048:
            return await reject(414, "request target too large")
        headers = scope.get("headers", [])
        if sum(len(k) + len(v) for k, v in headers) > 16_384:
            return await reject(431, "request headers too large")
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        transfer = [v for k, v in headers if k.lower() == b"transfer-encoding"]
        expected = None
        if lengths:
            value = lengths[0]
            if len(lengths) != 1 or transfer or not value or len(value) > 10 or not value.isdigit():
                return await reject(400, "invalid request framing")
            expected = int(value)
            if expected > MAX_BODY_BYTES:
                return await reject(413, "request body too large")
        if any(k.lower() == b"content-encoding" and v.lower() != b"identity" for k, v in headers):
            return await reject(415, "encoded request bodies are not supported")
        if scope["path"].startswith("/api/v1/"):
            peer = (scope.get("client") or ("unknown", 0))[0]
            try:
                await run_in_threadpool(consume_ingress, peer, scope["path"] in {
                    "/api/v1/register/challenge", "/api/v1/agents/register",
                })
            except AdmissionDenied as exc:
                return await reject(429, "request quota exceeded", {"Retry-After": str(exc.retry_after)})
            except Exception:
                # Do not leak DB errors/configuration, and never bypass a failed limiter.
                return await reject(503, "admission unavailable", {"Retry-After": "5"})
        body = bytearray()
        try:
            with anyio.fail_after(settings.body_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        return await reject(413, "request body too large")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "request body timeout")
        if expected is not None and expected != len(body):
            return await reject(400, "request length mismatch")
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        async def safe_send(message):
            if message["type"] == "http.response.start" and scope["path"].startswith("/api/v1/"):
                message = dict(message)
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", [])
                    if k.lower() not in {b"cache-control", b"x-server-timestamp"}
                ]
                message["headers"].append((b"cache-control", b"no-store"))
                # Authoritative server time, so an honest client can measure its
                # own drift against the tolerance instead of guessing. It is a
                # diagnostic: nothing server-side ever reads it back.
                message["headers"].append(
                    (b"x-server-timestamp", f"{clock.server_now():.6f}".encode("ascii"))
                )
            await send(message)

        await self.app(scope, replay_receive, safe_send)
