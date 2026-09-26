"""Single-source signing and registration helpers for the test suite.

Before this module the ``signed_request`` and ``register`` helpers were copied
into eight and seven test files respectively, and their signatures had already
drifted apart (some accepted a ``key``/``nonce`` override, some controlled the
client timestamp, one returned a ``(response, meta)`` tuple). Consolidating them
here removes that duplication while preserving the exact behaviour every caller
relied on, so the full suite passes unchanged.

The two public entry points are:

``signed_request(...)``
    Build and send an authenticated ``/api/v1`` request. Canonical request
    signing is over the path **without** its query string (the server verifies
    the same stripped path), so callers may pass a query string on ``path`` for
    the wire request while the signature stays stable. Optional keyword
    arguments cover every variation the suite needs:

    ``key``                 explicit ``Idempotency-Key`` (default: a fresh UUID)
    ``nonce``               explicit ``X-Agent-Nonce`` (default: a fresh UUID)
    ``timestamp``           explicit client instant as a float; formatted with
                            ``repr(float(...))`` so callers can probe the drift
                            window exactly (default: integer server seconds)
    ``signing_path``        override the path used for the signature only
    ``include_idempotency`` drop the ``Idempotency-Key`` header entirely
    ``return_meta``         also return the exact signed components as a dict

``register(...)``
    Complete the challenge/response registration handshake. ``manifest`` is
    optional and defaults to a minimal capability-free agent.
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes

#: Minimal manifest used when a caller does not supply one.
DEFAULT_MANIFEST: dict = {"name": "agent", "capabilities": [], "chains": ["base"]}


def signed_request(
    client: TestClient,
    identity: AgentIdentity,
    method: str,
    path: str,
    payload: dict,
    *,
    key: str | None = None,
    nonce: str | None = None,
    timestamp: float | None = None,
    signing_path: str | None = None,
    include_idempotency: bool = True,
    return_meta: bool = False,
):
    """Send a signed request; see the module docstring for the keyword options."""
    body = canonical_json(payload).encode()
    if timestamp is None:
        stamp = str(int(time.time()))
    else:
        stamp = repr(float(timestamp))
    nonce = nonce or uuid.uuid4().hex
    sign_path = signing_path if signing_path is not None else path.split("?", 1)[0]
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": stamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, sign_path, body, stamp, nonce)),
    }
    if include_idempotency:
        headers["Idempotency-Key"] = key or f"idem-{uuid.uuid4().hex}"
    response = client.request(method, path, content=body, headers=headers)
    if return_meta:
        return response, {
            "body": body,
            "timestamp": stamp,
            "nonce": nonce,
            "path": sign_path,
            "method": method,
        }
    return response


def register(client: TestClient, identity: AgentIdentity, manifest: dict | None = None) -> dict:
    """Run the registration challenge/response handshake and return the agent."""
    manifest = DEFAULT_MANIFEST if manifest is None else manifest
    challenge = client.get("/api/v1/register/challenge").json()
    payload = {
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "did": identity.did,
        "manifest": manifest,
    }
    payload["signature"] = identity.sign(
        registration_bytes(
            challenge["challenge_id"],
            challenge["nonce"],
            identity.did,
            manifest,
        )
    )
    response = client.post(
        "/api/v1/agents/register",
        content=canonical_json(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    return response.json()
