"""Resource retention and configuration-secret regressions from the broad audit."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from agentforge_server.adapters.technocore import TechnocoreAdapter
from agentforge_server.providers import MockInferenceProvider
from agentforge_server.schemas import InferenceRequestCreate
from agentforge_sdk.client import AgentIdentity
from test_outbox_regressions import audit_database, client
from test_audit_fixes import register, signed_request, create_task


@pytest.mark.parametrize("url", [
    "https://alice:PRIVATE_PASSWORD@example.test",
    "https://example.test/PRIVATE_PATH",
    "https://example.test?token=PRIVATE_QUERY",
    "https://example.test#PRIVATE_FRAGMENT",
])
def test_transport_status_never_logs_configured_url_secrets(url):
    adapter = TechnocoreAdapter(url, gossip_enabled=True, publish_path="/PRIVATE_PUBLISH_PATH")
    status = json.dumps(adapter.status())
    assert "PRIVATE" not in status
    assert "alice" not in status
    assert adapter.status()["base_url"] == "<configured>"
    assert adapter.base_url == url  # Redaction changes logging, not transport configuration.
    assert adapter.enabled


def test_mock_cache_is_bounded_by_count_and_preserves_recent_lookup(monkeypatch):
    provider = MockInferenceProvider()
    monkeypatch.setattr(provider, "MAX_CACHED_SESSIONS", 2)
    sessions = [asyncio.run(provider.create(InferenceRequestCreate())) for _ in range(3)]
    assert len(provider._sessions) == 2
    with pytest.raises(KeyError):
        asyncio.run(provider.status(sessions[0].id))
    assert asyncio.run(provider.status(sessions[2].id)) == "COMPLETED"
    assert asyncio.run(provider.result(sessions[2].id)) == sessions[2].result
    assert asyncio.run(provider.receipt(sessions[2].id)) == sessions[2].receipt
    asyncio.run(provider.cancel(sessions[2].id))
    assert asyncio.run(provider.status(sessions[2].id)) == "COMPLETED"


def test_mock_cache_byte_budget_and_oversized_entry(monkeypatch):
    provider = MockInferenceProvider()
    monkeypatch.setattr(provider, "MAX_CACHED_BYTES", 1500)
    for _ in range(5):
        asyncio.run(provider.create(InferenceRequestCreate(input_data={"text": "x" * 200})))
    assert 0 < len(provider._sessions) < 5
    assert provider._cached_bytes <= 1500
    original_bytes = provider._cached_bytes
    oversized = asyncio.run(provider.create(InferenceRequestCreate(input_data={"text": "x" * 2000})))
    assert oversized.status == "COMPLETED"
    assert oversized.id not in provider._sessions
    assert provider._cached_bytes == original_bytes


def test_mock_cache_concurrent_bookkeeping_is_bounded(monkeypatch):
    provider = MockInferenceProvider()
    monkeypatch.setattr(provider, "MAX_CACHED_SESSIONS", 8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: asyncio.run(provider.create(InferenceRequestCreate())), range(30)))
    assert len({result.id for result in results}) == 30
    assert len(provider._sessions) == 8
    assert provider._cached_bytes == sum(size for _, size in provider._sessions.values())
    assert provider._cached_bytes <= provider.MAX_CACHED_BYTES


def test_api_inference_survives_cache_eviction_with_private_read_auth(client, monkeypatch):
    import agentforge_server.providers as providers
    provider = MockInferenceProvider()
    monkeypatch.setattr(provider, "MAX_CACHED_SESSIONS", 1)
    monkeypatch.setattr(providers, "MOCK_PROVIDER", provider)
    poster, executor, outsider = (AgentIdentity.generate() for _ in range(3))
    for identity in (poster, executor, outsider):
        register(client, identity)
    from test_audit_fixes import task_payload
    task = create_task(client, poster, task_payload(visibility="private"))
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    responses = [signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/inference",
                                {"input_data": {"private_text": "private prompt"}}) for _ in range(2)]
    assert [response.status_code for response in responses] == [200, 200]
    first_id = responses[0].json()["session_id"]
    assert first_id not in provider._sessions
    path = f"/api/v1/inference/{first_id}"
    allowed = signed_request(client, executor, "GET", path, {}, include_idempotency=False)
    denied = signed_request(client, outsider, "GET", path, {}, include_idempotency=False)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["result"] == responses[0].json()["result"]
    assert denied.status_code == 404
    assert "private prompt" not in denied.text
