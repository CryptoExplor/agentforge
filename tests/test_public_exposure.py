"""D1-D6 regressions. Optional PostgreSQL uses isolated per-test schemas."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
from unittest.mock import Mock

from alembic import command
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text

from agentforge_sdk.client import AgentIdentity
from agentforge_server import db
from agentforge_server.admission import (
    AdmissionDenied, MAX_CLOCK_SKEW_SECONDS, consume, prune_security_state, validate_security_configuration,
)
from agentforge_server.app import create_app
from agentforge_server.middleware import MAX_BODY_BYTES, RequestSecurityMiddleware
from agentforge_server.models import Agent, RequestQuota, RegistrationChallenge, Task, UsedNonce, ValidationDecision
from agentforge_server.settings import settings
from agentforge_server.validators.result_schema import check_result_schema, UnsafeSchema, validate_result_schema
from test_audit_fixes import (
    register, signed_request, create_task, task_payload, setup_three_agents,
    submit, decision_payload,
)
from test_outbox_regressions import audit_database, migration_config


@pytest.fixture
def client(audit_database, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENV", "development")
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "registration_open", True)
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(audit_database)
    with TestClient(create_app()) as api:
        yield api


def _private_submission(client):
    poster, executor, reviewer, task = setup_three_agents(client, private=True)
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    inference = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/inference", {}).json()
    submission, _ = submit(client, task, executor, inference_session_ids=[inference["session_id"]])
    paths = [
        f"/api/v1/tasks/{task['id']}", f"/api/v1/inference/{inference['session_id']}",
        f"/api/v1/submissions/{submission['submission_id']}", f"/api/v1/proofs/{submission['submission_id']}",
    ]
    return poster, executor, reviewer, task, submission, paths


def test_d1_self_declared_validator_cannot_read_private_payloads(client):
    poster, executor, reviewer, task, submission, paths = _private_submission(client)
    attacker = AgentIdentity.generate()
    register(client, attacker)
    for capabilities in ([], ["validation"], ["validator"]):
        register(client, attacker, {"name": "untrusted", "capabilities": capabilities})
        for path in paths:
            response = signed_request(client, attacker, "GET", path, {})
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
    for identity in (poster, executor, reviewer):
        for path in paths:
            assert signed_request(client, identity, "GET", path, {}).status_code == 200
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ValidationDecision)) == 0


def test_d1_revocation_and_capability_removal_take_effect(client, monkeypatch):
    _, _, reviewer, _, _, paths = _private_submission(client)
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    for path in paths:
        assert signed_request(client, reviewer, "GET", path, {}).status_code == 404
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset({reviewer.did}))
    register(client, reviewer, {"name": "reviewer", "capabilities": []})
    assert signed_request(client, reviewer, "GET", paths[0], {}).status_code == 404


@pytest.mark.parametrize("resolve", [False, True])
def test_d1_decisions_and_idempotent_replay_require_current_grant(client, monkeypatch, resolve):
    poster, executor, reviewer, task, submission, _ = _private_submission(client)
    if resolve:
        opened = signed_request(client, poster, "POST", f"/api/v1/submissions/{submission['submission_id']}/disputes", {
            "dispute_id": "D_security", "reason": "Review this disputed submission",
        })
        assert opened.status_code == 200, opened.text
        path = "/api/v1/disputes/D_security/resolve"
    else:
        path = f"/api/v1/submissions/{submission['submission_id']}/validate"
    body = decision_payload(client, reviewer, submission)
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    assert signed_request(client, reviewer, "POST", path, body).status_code == 403
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ValidationDecision)) == 0
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset({reviewer.did}))
    first = signed_request(client, reviewer, "POST", path, body, key="grant-replay")
    assert first.status_code == 200, first.text
    assert signed_request(client, reviewer, "POST", path, body, key="grant-replay").json() == first.json()
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    assert signed_request(client, reviewer, "POST", path, body, key="grant-replay").status_code == 403
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ValidationDecision)) == 1


@pytest.mark.parametrize("ref", [
    "http://127.0.0.1/private", "https://schema.invalid/schema", "file:///etc/passwd",
    "ftp://schema.invalid/a", "relative.json", "//schema.invalid/a", "#", "#missing",
])
def test_d2_reference_retrieval_is_disabled(ref, monkeypatch):
    retrieval = Mock(side_effect=AssertionError("network must not be used"))
    monkeypatch.setattr("urllib.request.urlopen", retrieval)
    assert validate_result_schema({}, {"$ref": ref})[0] is False
    retrieval.assert_not_called()


@pytest.mark.parametrize("schema", [
    {"pattern": "(a+)+$"}, {"patternProperties": {"(a+)+$": {}}},
    {"allOf": [{"type": "object"}]}, {"uniqueItems": True},
    {"$dynamicRef": "https://schema.invalid"}, {"$id": "file:///tmp/schema"},
    {"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
    {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
    {"enum": list(range(257))}, {"type": "not-a-type"}, {"minimum": float("nan")},
])
def test_d2_unsupported_or_unbounded_schemas_fail_closed(schema):
    with pytest.raises(UnsafeSchema):
        check_result_schema(schema)
    assert validate_result_schema({}, schema)[0] is False


def test_d2_local_references_and_bounded_normal_validation():
    schema = {
        "$defs": {"answer": {"type": "string", "minLength": 1}},
        "type": "object", "properties": {"answer": {"$ref": "#/$defs/answer"}},
        "required": ["answer"], "additionalProperties": False,
    }
    assert validate_result_schema({"answer": "ok"}, schema) == (True, "")
    assert validate_result_schema({"answer": 1}, schema)[0] is False
    assert validate_result_schema({}, False)[0] is False
    assert validate_result_schema({}, True)[0] is True
    assert validate_result_schema({"secret": "DO_NOT_ECHO"}, {"const": {}}) == (
        False, "result does not satisfy acceptance schema",
    )
    assert validate_result_schema({"answer": "x" * 262_145}, schema)[0] is False
    deep = {}
    for _ in range(40):
        deep = {"x": deep}
    assert validate_result_schema(deep, True)[0] is False


@pytest.mark.parametrize("alias", ["result_schema", "output_schema", "schema"])
def test_d2_task_creation_rejects_unsafe_schema_before_task_write(client, alias):
    poster = AgentIdentity.generate()
    register(client, poster)
    payload = task_payload(acceptance={alias: {"$ref": "https://schema.invalid/secret"}})
    response = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
    assert response.status_code == 422
    assert "schema.invalid" not in response.text
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_d2_legacy_stored_unsafe_schema_fails_validation_without_fetch(client, monkeypatch):
    poster, executor, reviewer, task = setup_three_agents(client)
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    submission, _ = submit(client, task, executor)
    # Simulate existing pre-fix storage; public task creation now rejects this.
    with db.SessionLocal() as session:
        row = session.get(Task, task["id"])
        row.acceptance = {"result_schema": {"$ref": "file:///private"}}
        session.commit()
    retrieval = Mock(side_effect=AssertionError("no fetch"))
    monkeypatch.setattr("urllib.request.urlopen", retrieval)
    body = decision_payload(client, reviewer, submission)
    response = signed_request(client, reviewer, "POST", f"/api/v1/submissions/{submission['submission_id']}/validate", body)
    assert response.status_code == 422
    retrieval.assert_not_called()


def _middleware_probe(headers, chunks, *, stalled=False):
    async def run():
        captured, sent = [], []
        pending = list(chunks)
        async def receive():
            if stalled:
                await asyncio.sleep(1)
            if not pending:
                return {"type": "http.disconnect"}
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        async def send(message):
            sent.append(message)
        async def endpoint(scope, receive, send):
            captured.append((await receive())["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        await RequestSecurityMiddleware(endpoint)({
            "type": "http", "path": "/health", "method": "POST", "headers": headers,
        }, receive, send)
        statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
        return statuses, captured
    return asyncio.run(run())


@pytest.mark.parametrize("headers,status", [
    ([(b"content-length", b"2000001")], 413),
    ([(b"content-length", b"garbage")], 400),
    ([(b"content-length", b"-1")], 400),
    ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
    ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")], 400),
    ([(b"content-length", b"5")], 400),
    ([(b"content-encoding", b"gzip")], 415),
])
def test_d3_invalid_framing_is_client_error(headers, status):
    statuses, captured = _middleware_probe(headers, [b"x"])
    assert statuses == [status]
    assert captured == []


@pytest.mark.parametrize("headers", [[], [(b"transfer-encoding", b"chunked")], [(b"content-length", b"1")]])
def test_d3_streaming_limit_cannot_be_bypassed(headers):
    statuses, captured = _middleware_probe(headers, [b"a" * MAX_BODY_BYTES, b"b"])
    assert statuses == [413]
    assert not captured


def test_d3_exact_boundary_raw_bytes_preserved_and_disconnect():
    raw = b"a" * (MAX_BODY_BYTES - 2) + b"\n\x00"
    assert _middleware_probe([], [raw[:100], raw[100:]]) == ([200], [raw])
    assert _middleware_probe([], []) == ([], [])


def test_d3_slow_body_times_out(monkeypatch):
    monkeypatch.setattr(settings, "body_timeout_seconds", 0.01)
    assert _middleware_probe([], [b"x"], stalled=True) == ([408], [])


def test_d3_real_api_rejects_large_chunked_body_without_creating_task(client):
    response = client.post("/api/v1/tasks", content=iter([b"x" * MAX_BODY_BYTES, b"x"]))
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_d4_global_quota_shared_across_apps_and_failures(client, monkeypatch):
    monkeypatch.setattr(settings, "request_global_per_minute", 2)
    other = TestClient(create_app())
    try:
        assert client.get("/api/v1/does-not-exist").status_code == 404
        assert other.get("/api/v1/tasks").status_code == 200
        denied = client.get("/api/v1/tasks", headers={"X-Forwarded-For": "203.0.113.10"})
        assert denied.status_code == 429
        assert 1 <= int(denied.headers["retry-after"]) <= 60
        assert denied.headers["cache-control"] == "no-store"
        assert client.get("/health").status_code == 200
    finally:
        other.close()


def test_d4_registration_quota_counts_challenges_and_posts(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_ip_per_minute", 1)
    assert client.get("/api/v1/register/challenge").status_code == 200
    assert client.post("/api/v1/agents/register", json={}).status_code == 429
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(RegistrationChallenge)) == 1
        assert session.scalar(select(func.count()).select_from(Agent)) == 0


@pytest.mark.parametrize("complete_headers", [False, True])
def test_d4_signed_quota_cannot_be_charged_using_unverified_did(client, monkeypatch, complete_headers):
    identity = AgentIdentity.generate()
    register(client, identity)
    monkeypatch.setattr(settings, "request_did_per_minute", 1)
    path = "/api/v1/events"
    headers = {"X-Agent-DID": identity.did}
    if complete_headers:
        import uuid
        from agentforge_sdk.crypto import request_bytes
        stamp, nonce = str(time.time()), uuid.uuid4().hex
        headers.update({
            "X-Agent-Timestamp": stamp, "X-Agent-Nonce": nonce,
            "X-Agent-Signature": AgentIdentity.generate().sign(request_bytes("GET", path, b"", stamp, nonce)),
        })
    invalid = client.get(path, headers=headers)
    assert invalid.status_code == 401
    assert signed_request(client, identity, "GET", path, {}).status_code == 200
    assert signed_request(client, identity, "GET", path, {}).status_code == 429


def test_d4_admission_database_error_fails_closed_without_disclosure(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("password=DO_NOT_LEAK")
    monkeypatch.setattr("agentforge_server.middleware.consume_ingress", fail)
    response = client.get("/api/v1/register/challenge")
    assert response.status_code == 503 and "DO_NOT_LEAK" not in response.text
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(RegistrationChallenge)) == 0


def test_d4_concurrent_atomic_quota_and_window_rollover(client):
    def attempt(_):
        try:
            consume([("concurrent", "shared", 7)], timestamp=3600)
            return True
        except AdmissionDenied:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(30))) == 7
    with db.SessionLocal() as session:
        assert session.scalar(select(RequestQuota.count).where(RequestQuota.window == 60)) == 7
    consume([("concurrent", "shared", 7)], timestamp=3660)
    consume([("concurrent", "shared", 7)], timestamp=3780)
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(RequestQuota).where(RequestQuota.window < 62)) == 0


def test_d4_closed_enrollment_still_allows_existing_identity_updates(client, monkeypatch):
    existing = AgentIdentity.generate()
    register(client, existing)
    monkeypatch.setattr(settings, "registration_open", False)
    register(client, existing, {"name": "updated", "capabilities": ["validation"]})
    assert existing.did not in settings.trusted_validator_dids
    stranger = AgentIdentity.generate()
    from agentforge_sdk.crypto import registration_bytes
    challenge = client.get("/api/v1/register/challenge").json()
    manifest = {"name": "stranger"}
    payload = {"did": stranger.did, "manifest": manifest, "challenge_id": challenge["challenge_id"], "nonce": challenge["nonce"]}
    payload["signature"] = stranger.sign(registration_bytes(challenge["challenge_id"], challenge["nonce"], stranger.did, manifest))
    assert client.post("/api/v1/agents/register", json=payload).status_code == 403
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Agent)) == 1


def test_d4_security_retention_preserves_still_replayable_nonces(client):
    stamp = time.time()
    with db.SessionLocal() as session:
        session.add_all([
            UsedNonce(did="test", nonce="retain", created_at=stamp - 2 * MAX_CLOCK_SKEW_SECONDS),
            UsedNonce(did="test", nonce="old", created_at=stamp - 2 * MAX_CLOCK_SKEW_SECONDS - 61),
            RegistrationChallenge(challenge_id="old", nonce="old", created_at=0, expires_at=stamp-61, used=False),
        ])
        session.commit()
        prune_security_state(session, timestamp=stamp)
        assert session.scalars(select(UsedNonce.nonce)).all() == ["retain"]
        assert session.get(RegistrationChallenge, "old") is None


@pytest.mark.parametrize("field,value", [
    ("request_global_per_minute", 0), ("request_ip_per_minute", -1),
    ("body_timeout_seconds", float("inf")), ("trusted_validator_dids", frozenset({"not-a-did"})),
])
def test_d4_invalid_configuration_fails_closed(monkeypatch, field, value):
    monkeypatch.setattr(settings, field, value)
    with pytest.raises(ValueError):
        validate_security_configuration()


def test_d4_production_faucet_is_forbidden(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    with pytest.raises(ValueError, match="faucet"):
        validate_security_configuration()


def test_d5_search_is_not_shadowed_and_is_bounded(client):
    identities = [AgentIdentity.generate() for _ in range(3)]
    for i, identity in enumerate(identities):
        register(client, identity, {"name": str(i), "capabilities": ["research"], "chains": ["base"]})
    response = client.get("/api/v1/agents/search?capability=research&chain=base&limit=2")
    assert response.status_code == 200 and len(response.json()["agents"]) == 2
    assert client.get("/api/v1/agents/search?limit=101").status_code == 422
    assert client.get("/api/v1/agents/" + identities[0].did).json()["did"] == identities[0].did


@pytest.mark.parametrize("value", ["bad", "NaN", "sNaN", "Infinity", "-Infinity", "-1", "1e999999999", "1e-9999999", "9" * 81, ""])
def test_d6_invalid_reward_filter_is_422(client, value):
    assert client.get("/api/v1/tasks", params={"min_reward": value}).status_code == 422


@pytest.mark.parametrize("value", ["0", "0.00", "1.5", "1e2", "2E-3"])
def test_d6_valid_reward_filter_remains_supported(client, value):
    assert client.get("/api/v1/tasks", params={"min_reward": value}).status_code == 200


def test_d4_additive_quota_migration_preserves_outbox(audit_database, monkeypatch):
    config = migration_config(audit_database, monkeypatch)
    command.upgrade(config, "c4d5e6f7a8b9")
    db.configure_database(audit_database)
    with db.engine.begin() as connection:
        connection.execute(text("INSERT INTO outbox_events (id, kind, aggregate_id, payload, status, attempts, next_attempt_at, created_at) VALUES ('OUT_kept', 'TASK_CREATED', 'T_kept', '{}', 'PENDING', 2, 0, 1)"))
    with pytest.raises(RuntimeError, match="request_quotas"):
        db.verify_schema()
    command.upgrade(config, "head")
    db.verify_schema(require_migrations=True)
    consume([("migration", "test", 1)])
    command.downgrade(config, "c4d5e6f7a8b9")
    assert "request_quotas" not in inspect(db.engine).get_table_names()
    command.upgrade(config, "head")
    db.verify_schema(require_migrations=True)
    with db.engine.connect() as connection:
        assert connection.scalar(text("SELECT attempts FROM outbox_events WHERE id='OUT_kept'")) == 2


def test_d4_concurrency_limit_rejects_then_recovers(monkeypatch):
    monkeypatch.setattr(settings, "max_inflight_requests", 1)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def endpoint(scope, receive, send):
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
        middleware = RequestSecurityMiddleware(endpoint)
        scope = {"type": "http", "path": "/health", "headers": []}
        async def receive():
            return {"type": "http.request", "body": b""}
        first, second, third = [], [], []
        async def sink(target, message):
            target.append(message)
        from functools import partial
        running = asyncio.create_task(middleware(scope, receive, partial(sink, first)))
        await entered.wait()
        await middleware(scope, receive, partial(sink, second))
        assert second[0]["status"] == 429
        release.set()
        await running
        await middleware(scope, receive, partial(sink, third))
        assert first[0]["status"] == third[0]["status"] == 200
    asyncio.run(run())


def test_d3_headers_are_bounded():
    assert _middleware_probe([(b"x-agent-signature", b"a" * 16_385)], [b""])[0] == [431]


@pytest.mark.parametrize("value", ["1e999999999", "0e999999999", "1e-99999999"])
def test_d6_decimal_expansion_rejected_before_formatting(value):
    from pydantic import ValidationError
    from agentforge_server.schemas import Money, InferenceRequestCreate
    with pytest.raises(ValidationError):
        Money(amount=value)
    with pytest.raises(ValidationError):
        InferenceRequestCreate(requested_compute=value)


def test_d4_listing_response_budget_preserves_shape(client, monkeypatch):
    poster = AgentIdentity.generate()
    register(client, poster)
    for _ in range(3):
        create_task(client, poster)
    # Budget chosen to admit one normal task but not all three.
    first = client.get("/api/v1/tasks?limit=1").json()["tasks"][0]
    from agentforge_sdk.crypto import canonical_json
    monkeypatch.setattr("agentforge_server.app.MAX_LIST_BYTES", len(canonical_json(first).encode()) + 10)
    response = client.get("/api/v1/tasks")
    assert response.status_code == 200
    assert list(response.json()) == ["tasks"]
    assert len(response.json()["tasks"]) == 1


def test_d4_registration_challenge_is_atomic_under_concurrency(client):
    from agentforge_sdk.crypto import registration_bytes
    challenge = client.get("/api/v1/register/challenge").json()
    identities = [AgentIdentity.generate() for _ in range(2)]
    def attempt(identity):
        manifest = {"name": "concurrent"}
        payload = {"did": identity.did, "manifest": manifest, "challenge_id": challenge["challenge_id"], "nonce": challenge["nonce"]}
        payload["signature"] = identity.sign(registration_bytes(challenge["challenge_id"], challenge["nonce"], identity.did, manifest))
        return client.post("/api/v1/agents/register", json=payload).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attempt, identities))
    assert statuses.count(200) == 1
    assert all(status in {200, 400, 409} for status in statuses)
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Agent)) == 1


def test_d3_valid_chunked_signed_body_keeps_exact_signing_bytes(client):
    import uuid
    from agentforge_sdk.crypto import request_bytes
    poster = AgentIdentity.generate()
    register(client, poster)
    body = json.dumps(task_payload(), indent=2).encode()  # deliberately noncanonical bytes
    path, stamp, nonce = "/api/v1/tasks", str(time.time()), uuid.uuid4().hex
    headers = {"Content-Type": "application/json", "X-Agent-DID": poster.did,
               "X-Agent-Timestamp": stamp, "X-Agent-Nonce": nonce, "Idempotency-Key": nonce,
               "X-Agent-Signature": poster.sign(request_bytes("POST", path, body, stamp, nonce))}
    response = client.post(path, content=iter([body[:17], body[17:]]), headers=headers)
    assert response.status_code == 200, response.text


def test_d2_boolean_false_schema_is_enforced_in_acceptance(client):
    poster, executor, reviewer, task = setup_three_agents(client)
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    submission, _ = submit(client, task, executor)
    with db.SessionLocal() as session:
        session.get(Task, task["id"]).acceptance = {"result_schema": False}
        session.commit()
    body = decision_payload(client, reviewer, submission)
    assert signed_request(client, reviewer, "POST", f"/api/v1/submissions/{submission['submission_id']}/validate", body).status_code == 422


def test_d6_request_target_is_bounded(client):
    assert client.get("/api/v1/tasks?x=" + "a" * 8193).status_code == 414


def test_d2_nested_dialect_cannot_escape_keyword_budget():
    item = {
        'type': 'string', 'minLength': 0, 'maxLength': 10, 'enum': ['ok'], 'const': 'ok',
        'minimum': 0, 'maximum': 100, 'exclusiveMinimum': -1, 'exclusiveMaximum': 101,
        'multipleOf': 1, 'minItems': 0, 'maxItems': 256, 'minProperties': 0, 'maxProperties': 128,
        'properties': {}, 'required': [], 'additionalProperties': True, 'items': True,
    }
    schema = {'type': 'object', 'properties': {'values': {'type': 'array', 'items': item}}}
    result = {'values': ['ok'] * 256}
    assert validate_result_schema(result, schema)[0] is False  # keyword budget
    item['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    # jsonschema.evolve otherwise selects its ordinary (unbudgeted) validator.
    assert validate_result_schema(result, schema)[0] is False


def test_d2_root_dialect_and_literal_dialect_data_still_work():
    literal = {'$schema': 'this is instance data, not a schema declaration'}
    schema = {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'metadata': {'const': literal}},
    }
    assert validate_result_schema({'metadata': literal}, schema) == (True, '')


def test_d4_untrusted_forwarded_headers_cannot_reset_peer_quota(client, monkeypatch):
    monkeypatch.setattr(settings, 'request_ip_per_minute', 2)
    assert client.get('/api/v1/tasks').status_code == 200
    assert client.get('/api/v1/tasks', headers={'X-Forwarded-For': '203.0.113.1'}).status_code == 200
    denied = client.get('/api/v1/tasks', headers={'X-Forwarded-For': '203.0.113.2', 'Forwarded': 'for=203.0.113.3'})
    assert denied.status_code == 429
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(RequestQuota).where(RequestQuota.bucket.like('ip:%'))) == 1


def test_d4_prior_quota_charge_commits_when_later_quota_denies(client):
    global_bucket = ('verify-global', 'all', 2)
    consume([global_bucket, ('verify-peer', 'first', 1)], timestamp=3600)
    with pytest.raises(AdmissionDenied):
        consume([global_bucket, ('verify-peer', 'first', 1)], timestamp=3600)
    with pytest.raises(AdmissionDenied):
        consume([global_bucket, ('verify-peer', 'second', 1)], timestamp=3600)
    with db.SessionLocal() as session:
        assert session.scalar(select(RequestQuota.count).where(RequestQuota.bucket.like('verify-global:%'))) == 2
        # Rejected global admission cannot allocate a quota for a new peer.
        assert session.scalar(select(func.count()).select_from(RequestQuota)) == 2


def test_d4_signed_admission_works_with_a_single_pooled_connection(client, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    identity = AgentIdentity.generate()
    register(client, identity)
    kwargs = {'connect_args': {'check_same_thread': False}} if db.DATABASE_URL.startswith('sqlite') else {}
    engine = create_engine(db.DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=0.1, **kwargs)
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setattr(db, 'SessionLocal', sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    try:
        response = signed_request(client, identity, 'GET', '/api/v1/events', {})
        assert response.status_code == 200, response.text
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_postgres_production_startup_and_worker_without_publishing(audit_database, monkeypatch):
    """A fresh process tests production defaults, not a mutated dev singleton."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    if not audit_database.startswith('postgresql'):
        pytest.skip('requires AGENTFORGE_TEST_POSTGRES_URL for production startup')
    config = migration_config(audit_database, monkeypatch)
    command.upgrade(config, 'head')
    env = dict(os.environ)
    env.update({
        'AGENTFORGE_ENV': 'production', 'AGENTFORGE_DATABASE_URL': audit_database,
        'AGENTFORGE_AUTO_CREATE_SCHEMA': 'false', 'AGENTFORGE_ENABLE_MOCK_FAUCET': 'false',
        'AGENTFORGE_GOSSIP_ENABLED': 'false', 'AGENTFORGE_TECHNOCORE_PUBLISH_PATH': '',
        'AGENTFORGE_EVENT_SIGNING_KEY': '', 'AGENTFORGE_TRUSTED_VALIDATOR_DIDS': '',
    })
    env.pop('AGENTFORGE_REGISTRATION_OPEN', None)
    code = '''
from fastapi.testclient import TestClient
from agentforge_server.app import create_app
from agentforge_server.settings import settings
from agentforge_server.worker import prepare_database, run_once
from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import registration_bytes

assert not settings.registration_open and not settings.auto_create_schema
assert not settings.enable_mock_faucet and not settings.gossip_enabled
assert not settings.trusted_validator_dids
with TestClient(create_app()) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/api/v1/tasks').status_code == 200
    challenge = client.get('/api/v1/register/challenge').json()
    identity = AgentIdentity.generate()
    manifest = {'name': 'production-denial-probe'}
    body = dict(challenge_id=challenge['challenge_id'], nonce=challenge['nonce'],
                did=identity.did, manifest=manifest)
    body['signature'] = identity.sign(registration_bytes(challenge['challenge_id'], challenge['nonce'], identity.did, manifest))
    assert client.post('/api/v1/agents/register', json=body).status_code == 403
prepare_database()
assert run_once()['delivered'] == 0
print('PRODUCTION_STARTUP_AND_DISABLED_PUBLISHER_OK')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=45)
    # Do not echo connection configuration or subprocess errors containing URLs.
    assert result.returncode == 0, 'production startup/worker probe failed'
    assert 'PRODUCTION_STARTUP_AND_DISABLED_PUBLISHER_OK' in result.stdout
