"""SQL-backed task query and cursor pagination (MIMO roadmap 0.5).

Verifies that ``GET /api/v1/tasks`` evaluates filters, ordering and the
pagination window in SQL (no fetch-all-then-filter), that keyset cursors and
offset pagination are accurate and complete, and that legacy callers keep the
exact historical ``{"tasks": [...]}`` response shape.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.models import Task
from agentforge_server.settings import settings


@pytest.fixture(autouse=True)
def isolated_operator_policy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    monkeypatch.setattr(settings, "open_operators", True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'query.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


def signed_request(client: TestClient, identity: AgentIdentity, method: str, path: str, payload: dict):
    body = canonical_json(payload).encode()
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, path, body, timestamp, nonce)),
        "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
    }
    return client.request(method, path, content=body, headers=headers)


def register(client: TestClient, identity: AgentIdentity, manifest: dict | None = None) -> dict:
    manifest = manifest or {"name": "poster", "capabilities": ["research"], "chains": ["base"]}
    challenge = client.get("/api/v1/register/challenge").json()
    payload = {
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "did": identity.did,
        "manifest": manifest,
    }
    payload["signature"] = identity.sign(
        registration_bytes(challenge["challenge_id"], challenge["nonce"], identity.did, manifest)
    )
    response = client.post(
        "/api/v1/agents/register",
        content=canonical_json(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def make_task(
    *,
    kind: str = "research",
    origin: str = "research",
    strategy: str = "peer_review",
    capabilities: list[str] | None = None,
    chains: list[str] | None = None,
    reward: str = "1",
) -> dict:
    economics = (
        {"mode": "BOUNTY", "reward": {"amount": reward, "asset": "MOCK"}}
        if reward != "0"
        else {"mode": "REPUTATION", "reward": {"amount": "0", "asset": "MOCK"}}
    )
    return {
        "kind": kind,
        "visibility": "public",
        "origin": origin,
        "verification_strategy": strategy,
        "required_capabilities": capabilities or [],
        "chains": chains or ["base"],
        "input": {"question": "q"},
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 1},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": economics,
    }


def seeded_tasks(client: TestClient, count: int, factory=None):
    """Create tasks and pin strictly decreasing created_at for exact ordering.

    The i-th created task receives the i-th largest timestamp, so the API's
    ``(created_at DESC, id DESC)`` listing order equals creation order.
    """
    poster = AgentIdentity.generate()
    register(client, poster)
    ids = []
    for i in range(count):
        payload = factory(i) if factory else make_task()
        response = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    base = 1_000_000.0
    with db.SessionLocal() as session:
        for i, task_id in enumerate(ids):
            session.get(Task, task_id).created_at = base - i
            session.commit()
    return poster, ids


def listed_ids(client: TestClient, params: dict | None = None) -> list[str]:
    body = client.get("/api/v1/tasks", params=params).json()
    return [item["id"] for item in body["tasks"]]


# ---------------------------------------------------------------------------
# Pagination accuracy.
# ---------------------------------------------------------------------------


def test_limit_offset_pagination_pages_are_accurate_and_complete(client):
    _, ids = seeded_tasks(client, 25)

    page1 = client.get("/api/v1/tasks", params={"offset": 0, "limit": 10}).json()
    assert list(page1) == ["tasks", "total", "limit", "offset", "has_more", "next_cursor"]
    assert [item["id"] for item in page1["tasks"]] == ids[:10]
    assert page1["total"] == 25
    assert page1["has_more"] is True
    assert page1["offset"] == 0

    page2 = client.get("/api/v1/tasks", params={"offset": 10, "limit": 10}).json()
    assert [item["id"] for item in page2["tasks"]] == ids[10:20]

    page3 = client.get("/api/v1/tasks", params={"offset": 20, "limit": 10}).json()
    assert [item["id"] for item in page3["tasks"]] == ids[20:]
    assert page3["has_more"] is False
    assert page3["next_cursor"] is None

    seen = [item["id"] for page in (page1, page2, page3) for item in page["tasks"]]
    assert seen == ids  # complete, ordered, no duplicates


def test_offset_beyond_total_is_empty_page(client):
    seeded_tasks(client, 3)
    body = client.get("/api/v1/tasks", params={"offset": 100, "limit": 10}).json()
    assert body["tasks"] == []
    assert body["total"] == 3
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_cursor_pagination_walks_every_task_exactly_once(client):
    _, ids = seeded_tasks(client, 35)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 10, "offset": 0} if cursor is None else {"limit": 10, "cursor": cursor}
        body = client.get("/api/v1/tasks", params=params).json()
        seen.extend(item["id"] for item in body["tasks"])
        pages += 1
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        assert body["next_cursor"]
        cursor = body["next_cursor"]
        assert pages < 10  # termination guard

    assert seen == ids
    assert pages == 4  # 35 items at limit 10


def test_cursor_page_reports_remaining_total(client):
    _, ids = seeded_tasks(client, 12)
    legacy = client.get("/api/v1/tasks", params={"limit": 5}).json()
    assert list(legacy) == ["tasks"]
    cursor = client.get("/api/v1/tasks", params={"limit": 5, "offset": 0}).json()["next_cursor"]
    body = client.get("/api/v1/tasks", params={"cursor": cursor, "limit": 5}).json()
    assert body["total"] == 7  # remaining items after the cursor position
    assert [item["id"] for item in body["tasks"]] == ids[5:10]


def test_cursor_takes_precedence_over_offset(client):
    _, ids = seeded_tasks(client, 9)
    cursor = client.get("/api/v1/tasks", params={"limit": 4, "offset": 0}).json()["next_cursor"]
    combined = client.get("/api/v1/tasks", params={"cursor": cursor, "limit": 4, "offset": 999}).json()
    assert [item["id"] for item in combined["tasks"]] == ids[4:8]
    assert combined["offset"] == 0  # seek pagination, not offset


def test_invalid_cursor_is_rejected(client):
    response = client.get("/api/v1/tasks", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid task cursor"


def test_limit_bounds_are_enforced(client):
    seeded_tasks(client, 2)
    assert client.get("/api/v1/tasks", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/tasks", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/tasks", params={"limit": 100}).status_code == 200
    assert client.get("/api/v1/tasks", params={"offset": -1}).status_code == 422


def test_default_limit_is_50_and_legacy_shape_preserved(client):
    _, ids = seeded_tasks(client, 55)
    body = client.get("/api/v1/tasks").json()
    assert list(body) == ["tasks"]
    assert len(body["tasks"]) == 50
    created = [item["created_at"] for item in body["tasks"]]
    assert created == sorted(created, reverse=True)
    assert [item["id"] for item in body["tasks"]] == ids[:50]


def test_byte_budget_stops_without_skipping_tasks(client, monkeypatch):
    _, ids = seeded_tasks(client, 3)
    first = client.get("/api/v1/tasks", params={"limit": 1, "offset": 0}).json()["tasks"][0]
    monkeypatch.setattr(
        "agentforge_server.app.MAX_LIST_BYTES", len(canonical_json(first).encode()) + 10
    )
    body = client.get("/api/v1/tasks", params={"limit": 10, "offset": 0}).json()
    assert len(body["tasks"]) == 1  # budget admits one task
    assert body["has_more"] is True
    # Walking budget-limited pages must still cover every task exactly once:
    # each cursor continues from the last rendered item, so nothing is skipped.
    walked = [body["tasks"][0]["id"]]
    cursor = body["next_cursor"]
    while cursor is not None:
        page = client.get("/api/v1/tasks", params={"cursor": cursor, "limit": 10}).json()
        walked.extend(item["id"] for item in page["tasks"])
        cursor = page["next_cursor"]
    assert walked == ids


# ---------------------------------------------------------------------------
# Filter accuracy (SQL-side).
# ---------------------------------------------------------------------------


KINDS = ["expert", "research", "service", "research"]


def filter_factory(i: int) -> dict:
    return [
        make_task(kind="expert", origin="external", capabilities=["proxy_security"], chains=["base"], reward="5"),
        make_task(kind="research", origin="research", capabilities=["security"], chains=["optimism"], reward="2"),
        make_task(kind="service", origin="agent_service", strategy="deterministic", capabilities=[], chains=["base"], reward="10"),
        make_task(kind="research", origin="research", strategy="operator", capabilities=["security", "extra"], chains=["base"], reward="0"),
    ][i % 4]


@pytest.fixture
def filter_universe(client):
    """Eight tasks cycling four known attribute templates (indexes 0..7)."""
    _, ids = seeded_tasks(client, 8, filter_factory)
    return ids


def test_kind_filter_matches_exactly(client, filter_universe):
    ids = filter_universe
    assert listed_ids(client, {"kind": "expert"}) == [ids[i] for i in (0, 4)]
    assert listed_ids(client, {"kind": "service"}) == [ids[i] for i in (2, 6)]


def test_verification_strategy_filter(client, filter_universe):
    ids = filter_universe
    assert listed_ids(client, {"verification_strategy": "deterministic"}) == [ids[i] for i in (2, 6)]
    assert listed_ids(client, {"verification_strategy": "operator"}) == [ids[i] for i in (3, 7)]


def test_capability_filter_is_exact_not_substring(client, filter_universe):
    ids = filter_universe
    # "security" must not match the "proxy_security"-only task.
    assert listed_ids(client, {"capability": "security"}) == [ids[i] for i in (1, 3, 5, 7)]
    assert listed_ids(client, {"capability": "proxy_security"}) == [ids[i] for i in (0, 4)]


def test_chain_filter(client, filter_universe):
    ids = filter_universe
    assert listed_ids(client, {"chain": "optimism"}) == [ids[i] for i in (1, 5)]


def test_origin_filter(client, filter_universe):
    ids = filter_universe
    assert listed_ids(client, {"origin": "external"}) == [ids[i] for i in (0, 4)]


def test_min_reward_filter(client, filter_universe):
    ids = filter_universe
    assert listed_ids(client, {"min_reward": "5"}) == [ids[i] for i in (0, 2, 4, 6)]
    assert listed_ids(client, {"min_reward": "2.5"}) == [ids[i] for i in (0, 2, 4, 6)]
    assert listed_ids(client, {"min_reward": "10"}) == [ids[i] for i in (2, 6)]
    # A zero threshold keeps zero-reward (REPUTATION) tasks visible.
    assert listed_ids(client, {"min_reward": "0"}) == ids


def test_status_filter_with_dedicated_poster(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    keep = signed_request(client, poster, "POST", "/api/v1/tasks", make_task())
    cancel = signed_request(client, poster, "POST", "/api/v1/tasks", make_task())
    assert keep.status_code == 200 and cancel.status_code == 200
    cancelled = signed_request(client, poster, "POST", f"/api/v1/tasks/{cancel.json()['id']}/cancel", {})
    assert cancelled.status_code == 200, cancelled.text
    assert listed_ids(client, {"status": "CANCELLED"}) == [cancel.json()["id"]]
    assert listed_ids(client, {"status": "FUNDED"}) == [keep.json()["id"]]
    # Without a status filter both public tasks remain listed (unchanged legacy semantics).
    assert set(listed_ids(client)) == {keep.json()["id"], cancel.json()["id"]}


def test_combined_filters_and_metadata(client, filter_universe):
    ids = filter_universe
    body = client.get(
        "/api/v1/tasks",
        params={"kind": "research", "chain": "base", "min_reward": "0", "offset": 0, "limit": 100},
    ).json()
    assert [item["id"] for item in body["tasks"]] == [ids[i] for i in (3, 7)]
    assert body["total"] == 2
    assert body["has_more"] is False


def test_private_tasks_are_excluded_by_sql(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    private = signed_request(client, poster, "POST", "/api/v1/tasks", {**make_task(), "visibility": "private"})
    assert private.status_code == 200, private.text
    public = signed_request(client, poster, "POST", "/api/v1/tasks", make_task())
    assert public.status_code == 200, public.text
    assert listed_ids(client) == [public.json()["id"]]


# ---------------------------------------------------------------------------
# SQL-backed evidence: no fetch-all scan behind the endpoint.
# ---------------------------------------------------------------------------


def test_listing_evaluates_pagination_in_sql(client):
    seeded_tasks(client, 120)
    statements: list[tuple[str, object]] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "from tasks" in lowered and "select" in lowered:
            statements.append((statement, parameters))

    engine = db.engine
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        body = client.get("/api/v1/tasks", params={"limit": 50, "offset": 0}).json()
        assert len(body["tasks"]) == 50 and body["total"] == 120
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    # The page query and the count query are the only SELECTs against tasks.
    assert len(statements) <= 3
    # The window is enforced in SQL: LIMIT 51 (page + lookahead), never the
    # historical fetch-500 scan.
    flattened = json.dumps([str(params) for _, params in statements])
    assert "500" not in flattened
    assert "51" in flattened
