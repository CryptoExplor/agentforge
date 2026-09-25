"""Phase 1.4 — server-anchored timestamps and clock-drift defence.

Covers the Grok roadmap 1.4 invariants:

1. a signed request whose ``X-Agent-Timestamp`` sits outside the strict drift
   window around the *server* clock is rejected with 401
   ``client clock drift exceeds tolerance``;
2. every lease, claim expiry and submission deadline is computed from the
   server's own ``received_at`` — a client clock (fast, slow or jumping) can
   never move one;
3. the authoritative clock never rewinds, so a host clock set backwards cannot
   extend an in-flight lease, and deadline decisions fail closed when the API
   host clock and the database clock disagree;
4. those invariants hold under concurrency and simulated handler latency.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, request_bytes
from agentforge_server import clock, db
from agentforge_server.app import CLAIM_LEASE_SECONDS, create_app
from agentforge_server.models import Claim, Dispute, Submission, Task, UsedNonce
from agentforge_server.settings import settings
from agentforge_server.validators import validate_submission

from test_audit_fixes import (
    create_task,
    register,
    submit_payload,
    task_payload,
)

# A request is stamped at ingress and checked a few milliseconds later, so the
# accepted edge leaves a second of slack and the rejected edge leaves a second
# of margin on the other side of the 60-second default.
ACCEPTED_OFFSETS = [-58, -30, -1, 0, 1, 30, 58]
REJECTED_OFFSETS = [-3600, -300, -61, 61, 300, 3600]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'clock.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


def signed_request(
    client: TestClient,
    identity: AgentIdentity,
    method: str,
    path: str,
    payload: dict,
    *,
    timestamp: float | None = None,
    nonce: str | None = None,
    key: str | None = None,
    include_idempotency: bool = True,
):
    """Signed request whose client timestamp the test controls exactly."""
    body = canonical_json(payload).encode()
    stamp = repr(float(time.time() if timestamp is None else timestamp))
    nonce = nonce or uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": stamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, path, body, stamp, nonce)),
    }
    if include_idempotency:
        headers["Idempotency-Key"] = key or f"idem-{uuid.uuid4().hex}"
    return client.request(method, path, content=body, headers=headers)


def claim_task(client, executor: AgentIdentity, task: dict, **kwargs):
    response = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}, **kwargs
    )
    assert response.status_code == 200, response.text
    return response.json()


def stored_claim(claim_id: str) -> Claim:
    with db.SessionLocal() as session:
        claim = session.get(Claim, claim_id)
        session.expunge(claim)
        return claim


def set_task_deadline(task_id: str, deadline: float) -> None:
    """Move a task's deadline directly in the database, on server time.

    Deadline tests create the task with **no** deadline and set it afterwards,
    so the boundary is a fact the test controls rather than a race against how
    long registration, task creation, claiming and submitting happened to take
    under full-suite load. A task created with ``deadline = now + 2`` expires
    mid-setup on a slow runner and fails the request being tested.
    """
    with db.SessionLocal() as session:
        task = session.get(Task, task_id)
        task.deadline = deadline
        session.commit()


def two_agents(client):
    poster, executor = AgentIdentity.generate(), AgentIdentity.generate()
    register(client, poster)
    register(client, executor, {"name": "executor", "capabilities": [], "chains": ["base"]})
    return poster, executor


# ---------------------------------------------------------------------------
# 1. Clock-skew rejection
# ---------------------------------------------------------------------------


def test_default_drift_tolerance_is_sixty_seconds():
    assert settings.request_clock_skew_seconds == 60


@pytest.mark.parametrize("offset", REJECTED_OFFSETS)
def test_request_outside_the_drift_window_is_rejected(client, offset):
    poster, _ = two_agents(client)
    stamp = time.time() + offset
    response = signed_request(
        client, poster, "POST", "/api/v1/tasks", task_payload(), timestamp=stamp
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "client clock drift exceeds tolerance"
    # A rejected request creates no state at all: no task, no consumed nonce.
    with db.SessionLocal() as session:
        assert session.scalars(select(Task)).all() == []
        assert session.scalars(select(UsedNonce)).all() == []


@pytest.mark.parametrize("offset", ACCEPTED_OFFSETS)
def test_request_inside_the_drift_window_is_accepted(client, offset):
    poster, _ = two_agents(client)
    response = signed_request(
        client, poster, "POST", "/api/v1/tasks", task_payload(), timestamp=time.time() + offset
    )
    assert response.status_code == 200, response.text


def test_drift_window_is_configurable_and_fails_closed(client, monkeypatch):
    poster, _ = two_agents(client)
    monkeypatch.setattr(settings, "request_clock_skew_seconds", 5)
    late = signed_request(
        client, poster, "POST", "/api/v1/tasks", task_payload(), timestamp=time.time() - 10
    )
    assert late.status_code == 401
    assert late.json()["detail"] == "client clock drift exceeds tolerance"
    fresh = signed_request(
        client, poster, "POST", "/api/v1/tasks", task_payload(), timestamp=time.time()
    )
    assert fresh.status_code == 200, fresh.text


def test_futuristic_timestamp_cannot_pre_date_a_lease(client):
    """A clock an hour ahead is outside the window, so it earns nothing."""
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    response = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/claim",
        {},
        timestamp=time.time() + 3600,
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "client clock drift exceeds tolerance"
    with db.SessionLocal() as session:
        assert session.scalars(select(Claim)).all() == []
        assert session.get(Task, task["id"]).status == "OPEN"


def test_unparseable_timestamps_are_still_rejected(client):
    poster, _ = two_agents(client)
    for stamp in ("not-a-number", "", "nan", "inf", "-inf"):
        nonce = uuid.uuid4().hex
        body = canonical_json(task_payload()).encode()
        response = client.post(
            "/api/v1/tasks",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Agent-DID": poster.did,
                "X-Agent-Timestamp": stamp,
                "X-Agent-Nonce": nonce,
                "X-Agent-Signature": poster.sign(
                    request_bytes("POST", "/api/v1/tasks", body, stamp, nonce)
                ),
                "Idempotency-Key": nonce,
            },
        )
        assert response.status_code == 401


def test_server_timestamp_header_lets_an_honest_client_resynchronise(client):
    response = client.get("/api/v1/tasks")
    assert response.status_code == 200
    stamp = float(response.headers["x-server-timestamp"])
    assert abs(stamp - time.time()) <= settings.request_clock_skew_seconds


def test_health_reports_the_authoritative_clock(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    reported = body["clock"]
    assert reported["source"] == "monotonic-anchored"
    assert reported["drift_tolerance_seconds"] == settings.request_clock_skew_seconds
    assert abs(reported["server_time"] - time.time()) <= settings.request_clock_skew_seconds
    assert abs(reported["database_skew_seconds"]) < 1


# ---------------------------------------------------------------------------
# 2. The authoritative clock itself
# ---------------------------------------------------------------------------


def test_authoritative_clock_is_monotonic_under_load():
    samples = [clock.server_now() for _ in range(2000)]
    assert samples == sorted(samples)
    assert all(sample > 0 for sample in samples)


def test_authoritative_clock_never_rewinds_when_the_host_clock_steps_back(monkeypatch):
    monkeypatch.setattr(clock, "_epoch_base", clock._epoch_base)
    monkeypatch.setattr(clock, "_last_issued", clock._last_issued)
    real_time = time.time
    before = clock.server_now()
    monkeypatch.setattr(clock.time, "time", lambda: real_time() - 3600)
    after = clock.server_now()
    # A rewound host clock cannot hand out an earlier instant, so it cannot
    # lengthen a lease that is already running.
    assert after >= before


def test_authoritative_clock_resyncs_forward_after_a_suspend(monkeypatch):
    monkeypatch.setattr(clock, "_epoch_base", clock._epoch_base)
    monkeypatch.setattr(clock, "_last_issued", clock._last_issued)
    real_time = time.time
    # time.monotonic() does not advance while a machine is suspended, so the
    # anchored clock must snap forward or every honest client looks futuristic.
    monkeypatch.setattr(clock.time, "time", lambda: real_time() + 3600)
    jumped = clock.server_now()
    assert jumped >= real_time() + 3600 - clock.WALL_RESYNC_THRESHOLD_SECONDS
    monkeypatch.setattr(clock.time, "time", lambda: real_time() - 3600)
    assert clock.server_now() >= jumped


def test_lease_expiry_is_derived_only_from_a_server_anchored_receipt():
    from agentforge_server.services import lease_expiry

    assert lease_expiry(1_000.0, 900.0) == 1_900.0
    for received, seconds in [(0, 900), (-5, 900), (float("nan"), 900), (float("inf"), 900),
                              (1_000.0, 0), (1_000.0, -1), (1_000.0, float("nan")),
                              (None, 900), ("1000", 900), (True, 900)]:
        with pytest.raises(ValueError):
            lease_expiry(received, seconds)


def test_database_server_clock_is_readable_and_close_to_the_host_clock(client):
    with db.SessionLocal() as session:
        database_time = clock.database_now(session)
        skew = clock.database_skew_seconds(session)
        # A second, independent SQL round-trip: same clock source, so the two
        # readings can only differ by the time between the queries.
        reference = clock.deadline_reference(session)
    assert abs(database_time - time.time()) < 5
    assert abs(skew) < 5
    assert abs(reference - database_time) < 1.0


def test_deadline_reference_fails_closed_beyond_the_configured_tolerance(client, monkeypatch):
    with db.SessionLocal() as session:
        assert clock.deadline_reference(session, tolerance=60) == pytest.approx(time.time(), abs=5)
        monkeypatch.setattr(clock, "database_now", lambda _session: time.time() + 30)
        with pytest.raises(clock.ClockUnavailable, match="not synchronized"):
            clock.deadline_reference(session, tolerance=5)
        # A database clock that cannot be read at all is also refused, not guessed.
        def unreadable(_session):
            raise clock.ClockUnavailable("database server time is unavailable")

        monkeypatch.setattr(clock, "database_now", unreadable)
        with pytest.raises(clock.ClockUnavailable):
            clock.deadline_reference(session, tolerance=5)


def test_unsupported_dialect_has_no_server_time_expression():
    with pytest.raises(clock.ClockUnavailable):
        clock.database_now_expression("mysql")


# ---------------------------------------------------------------------------
# 3. Lease invariants: a client clock can never move an expiry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [-58, 0, 58])
def test_claim_lease_is_anchored_to_server_receipt_not_the_client_clock(client, offset):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    stamp = time.time() + offset
    claimed = claim_task(client, executor, task, timestamp=stamp)

    claim = stored_claim(claimed["claim_id"])
    # The lease is exactly received_at + lease, and received_at is the server's
    # own receipt time, so the client clock bought (or lost) nothing.
    assert claim.lease_expires_at == pytest.approx(claim.received_at + CLAIM_LEASE_SECONDS, abs=1e-6)
    assert claim.heartbeat_at == claim.received_at == claim.created_at == claim.updated_at
    assert claim.lease_expires_at <= clock.server_now() + CLAIM_LEASE_SECONDS + 1
    assert claim.lease_expires_at >= clock.server_now() + CLAIM_LEASE_SECONDS - 5
    assert claimed["lease_expires_at"] == pytest.approx(claim.lease_expires_at, abs=1e-6)
    assert claimed["received_at"] == pytest.approx(claim.received_at, abs=1e-6)
    if offset > 0:
        # A clock running 58s ahead did not buy 58s of extra lease.
        assert claim.lease_expires_at < stamp + CLAIM_LEASE_SECONDS


def test_heartbeat_extends_the_lease_from_server_receipt_only(client):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claimed = claim_task(client, executor, task)
    original = stored_claim(claimed["claim_id"])

    response = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/claims/{claimed['claim_id']}/heartbeat",
        {},
        timestamp=time.time() + 58,
    )
    assert response.status_code == 200, response.text
    extended = stored_claim(claimed["claim_id"])

    assert extended.received_at >= original.received_at
    assert extended.heartbeat_at == extended.received_at
    assert extended.lease_expires_at == pytest.approx(
        extended.received_at + CLAIM_LEASE_SECONDS, abs=1e-6
    )
    assert extended.lease_expires_at <= clock.server_now() + CLAIM_LEASE_SECONDS + 1
    assert response.json()["lease_expires_at"] == pytest.approx(extended.lease_expires_at, abs=1e-6)


def test_repeated_heartbeats_cannot_accumulate_beyond_one_lease(client):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claimed = claim_task(client, executor, task)
    received = []
    for offset in (58, -58, 0, 58, -58):
        response = signed_request(
            client,
            executor,
            "POST",
            f"/api/v1/claims/{claimed['claim_id']}/heartbeat",
            {},
            timestamp=time.time() + offset,
        )
        assert response.status_code == 200, response.text
        received.append(response.json()["received_at"])
    # received_at comes from the monotonic server clock: it never rewinds, no
    # matter how the client clock jumps between requests.
    assert received == sorted(received)
    claim = stored_claim(claimed["claim_id"])
    assert claim.received_at == pytest.approx(received[-1], abs=1e-6)
    assert claim.lease_expires_at == pytest.approx(claim.received_at + CLAIM_LEASE_SECONDS, abs=1e-6)
    assert claim.lease_expires_at <= clock.server_now() + CLAIM_LEASE_SECONDS + 1


def test_inference_extends_the_lease_from_the_request_receipt(client):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claimed = claim_task(client, executor, task)
    before = clock.server_now()
    response = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/inference",
        {"input_data": {"text": "prompt"}},
        timestamp=time.time() + 58,
    )
    assert response.status_code == 200, response.text
    claim = stored_claim(claimed["claim_id"])
    assert claim.received_at >= before
    assert claim.lease_expires_at == pytest.approx(claim.received_at + CLAIM_LEASE_SECONDS, abs=1e-6)
    assert claim.lease_expires_at <= clock.server_now() + CLAIM_LEASE_SECONDS + 1


def test_expired_lease_is_reaped_on_server_time_and_cannot_be_heartbeated(client):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claimed = claim_task(client, executor, task)
    with db.SessionLocal() as session:
        claim = session.get(Claim, claimed["claim_id"])
        claim.lease_expires_at = clock.server_now() - 1
        session.commit()

    response = signed_request(
        client, executor, "POST", f"/api/v1/claims/{claimed['claim_id']}/heartbeat", {},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "claim is no longer active"
    # The reaper expired it on server time; no client clock can revive it.
    with db.SessionLocal() as session:
        assert session.get(Claim, claimed["claim_id"]).status == "EXPIRED"
        assert session.get(Task, task["id"]).status == "OPEN"


# ---------------------------------------------------------------------------
# 4. Submission deadlines and dispute windows on server time
# ---------------------------------------------------------------------------


def test_submission_records_the_server_receipt_time(client):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claim_task(client, executor, task)
    declared = time.time() - 500  # executor back-dates its own proof
    payload = submit_payload(client, task, executor, created_at=declared)
    before = clock.server_now()
    response = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )
    assert response.status_code == 200, response.text

    with db.SessionLocal() as session:
        submission = session.get(Submission, payload["submission_id"])
        assert submission.created_at == pytest.approx(declared, abs=1)
        # The stored proof keeps the declared instant; the server anchor does not.
        assert submission.received_at >= before
        assert submission.received_at > submission.created_at


def test_back_dated_created_at_cannot_beat_the_server_deadline(client):
    poster, executor = two_agents(client)
    # No deadline while the claim is taken: setup latency can never expire the
    # task underneath the request being tested.
    task = create_task(client, poster)
    claim_task(client, executor, task)
    # Deadline deterministically in the past — no sleeping, no wall-clock race.
    set_task_deadline(task["id"], clock.server_now() - 10)

    payload = submit_payload(client, task, executor, created_at=time.time() - 600)
    response = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "task deadline has passed"
    with db.SessionLocal() as session:
        assert session.get(Submission, payload["submission_id"]) is None
        assert session.get(Task, task["id"]).status == "EXPIRED"


@pytest.mark.parametrize("received_at,expected", [(-100.0, "PASS"), (100.0, "FAIL")])
def test_deterministic_deadline_check_uses_received_at_not_the_declared_time(received_at, expected):
    """A back-dated proof cannot make a late submission look early."""
    deadline = 1_000_000.0
    task = Task(
        id="T_deadline",
        acceptance={"required_outputs": ["answer"]},
        acceptance_hash="unused",
        input_data={},
        deadline=deadline,
    )
    submission = Submission(
        id="S_deadline",
        task_id=task.id,
        result={"answer": "ok"},
        evidence=[],
        inference_session_ids=[],
        proof={},
        result_hash="unused",
        proof_hash="unused",
        # Always declared *before* the deadline; only the server anchor moves.
        created_at=deadline - 500.0,
        received_at=deadline + received_at,
    )
    checks = {item["code"]: item["result"] for item in validate_submission(None, task, submission).checks}
    assert checks["SUBMISSION_BEFORE_DEADLINE"] == expected


def test_dispute_window_is_evaluated_on_server_time(client, monkeypatch):
    poster, executor = two_agents(client)
    # Create with no deadline so registration, task creation, claim and
    # submission can take as long as the runner needs; the deadline that closes
    # the window is set in the database afterwards.
    task = create_task(client, poster)
    claim_task(client, executor, task)
    payload = submit_payload(client, task, executor)
    assert signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    ).status_code == 200

    # Deadline in the past plus a zero grace period closes the window
    # deterministically — no sleeping, no dependence on setup duration.
    set_task_deadline(task["id"], clock.server_now() - 10)
    monkeypatch.setattr(settings, "dispute_window_seconds", 0)
    dispute = {
        "dispute_id": f"D_{uuid.uuid4().hex}",
        "reason": "The acceptance interpretation needs an independent review.",
        "additional_evidence": [],
    }
    # A client clock running slow (inside the tolerance) claims less time has
    # passed, i.e. that the window should still be open. The server disagrees.
    closed = signed_request(
        client, poster, "POST", f"/api/v1/submissions/{payload['submission_id']}/disputes",
        dispute, timestamp=time.time() - 58,
    )
    assert closed.status_code == 409
    assert closed.json()["detail"] == "dispute window has closed"
    with db.SessionLocal() as session:
        assert session.get(Submission, payload["submission_id"]).status == "SUBMITTED"


def test_dispute_inside_the_window_still_opens(client, monkeypatch):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claim_task(client, executor, task)
    payload = submit_payload(client, task, executor)
    assert signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    ).status_code == 200

    # Same past deadline as the closed case; only the grace period differs, so
    # the pair isolates the window itself rather than elapsed wall time.
    set_task_deadline(task["id"], clock.server_now() - 10)
    monkeypatch.setattr(settings, "dispute_window_seconds", 3600)
    dispute = {
        "dispute_id": f"D_{uuid.uuid4().hex}",
        "reason": "The acceptance interpretation needs an independent review.",
        "additional_evidence": [],
    }
    # A client clock running fast claims *more* time has passed, i.e. that the
    # window should be closed. The server's own clock says otherwise.
    opened = signed_request(
        client, poster, "POST", f"/api/v1/submissions/{payload['submission_id']}/disputes",
        dispute, timestamp=time.time() + 58,
    )
    assert opened.status_code == 200, opened.text
    with db.SessionLocal() as session:
        row = session.get(Dispute, dispute["dispute_id"])
        # The dispute is stamped with server time, not the disputer's clock.
        assert row.created_at <= clock.server_now()
        assert row.created_at > time.time() - 60


def test_deadline_decisions_fail_closed_when_the_clocks_disagree(client, monkeypatch):
    poster, executor = two_agents(client)
    task = create_task(client, poster, task_payload(deadline=time.time() + 3600))
    # Claim while the two clocks still agree, so the divergence below is the
    # only thing that can fail the request.
    claimed = claim_task(client, executor, task)

    monkeypatch.setattr(settings, "db_clock_skew_tolerance_seconds", 0.1)
    real_database_now = clock.database_now
    monkeypatch.setattr(clock, "database_now", lambda session: real_database_now(session) + 120)

    payload = submit_payload(client, task, executor)
    response = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )
    # Neither clock is trusted over the other: the decision refuses to settle.
    assert response.status_code == 503
    assert response.json()["detail"] == "server clock is not synchronized"
    with db.SessionLocal() as session:
        assert session.get(Submission, payload["submission_id"]) is None
        assert session.get(Claim, claimed["claim_id"]).status == "ACTIVE"

    # The same guard covers every other deadline-bearing decision, not just
    # submission: a heartbeat on the same task also refuses to guess.
    heartbeat = signed_request(
        client, executor, "POST", f"/api/v1/claims/{claimed['claim_id']}/heartbeat", {}
    )
    assert heartbeat.status_code == 503
    assert heartbeat.json()["detail"] == "server clock is not synchronized"


def test_a_task_without_a_deadline_never_depends_on_the_database_clock(client, monkeypatch):
    poster, executor = two_agents(client)

    def explode(session):  # pragma: no cover - must not be called
        raise AssertionError("deadline-free tasks must not read the database clock")

    monkeypatch.setattr(clock, "database_now", explode)
    task = create_task(client, poster)
    claim_task(client, executor, task)
    payload = submit_payload(client, task, executor)
    response = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )
    assert response.status_code == 200, response.text


def test_configuration_rejects_an_unsafe_clock_setup(monkeypatch):
    from agentforge_server.admission import validate_security_configuration

    for field, value in [
        ("request_clock_skew_seconds", 0),
        ("request_clock_skew_seconds", -1),
        ("db_clock_skew_tolerance_seconds", 0),
        ("db_clock_skew_tolerance_seconds", float("nan")),
        ("db_clock_skew_tolerance_seconds", 600),
        ("dispute_window_seconds", -1),
        ("dispute_window_seconds", 31 * 24 * 60 * 60),
    ]:
        monkeypatch.setattr(settings, field, value)
        with pytest.raises(ValueError):
            validate_security_configuration()


# ---------------------------------------------------------------------------
# 5. Concurrency and simulated latency
# ---------------------------------------------------------------------------


def test_concurrent_claims_produce_exactly_one_server_anchored_lease(client):
    """Racing claims yield one lease, anchored to the winner's server receipt.

    Two racers, matching the repository's other HTTP write races: SQLite (the
    dev/test default) has a single writer, and a wider field raises
    "database is locked" on the unmodified baseline too. The invariant under
    test is the SQL claim guard, which PostgreSQL — the required production
    database — enforces for a wider field in the same way.
    """
    poster = AgentIdentity.generate()
    register(client, poster)
    executors = []
    for index in range(2):
        executor = AgentIdentity.generate()
        register(client, executor, {"name": f"executor-{index}", "capabilities": [], "chains": ["base"]})
        executors.append(executor)
    task = create_task(client, poster)

    def attempt(executor):
        # Each racer also lies about its clock, inside the tolerated window.
        return signed_request(
            client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {},
            timestamp=time.time() + 58,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt, executors))
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1
    assert set(statuses) <= {200, 409}

    with db.SessionLocal() as session:
        active = session.scalars(select(Claim).where(Claim.status == "ACTIVE")).all()
        assert len(active) == 1
        claim = active[0]
        assert claim.lease_expires_at == pytest.approx(claim.received_at + CLAIM_LEASE_SECONDS, abs=1e-6)
        assert claim.lease_expires_at <= clock.server_now() + CLAIM_LEASE_SECONDS + 1


def test_slow_handler_cannot_push_the_lease_past_the_receipt_anchor(client, monkeypatch):
    """Simulated latency: the lease stays anchored to ingress, not completion."""
    poster, executor = two_agents(client)
    task = create_task(client, poster)

    from agentforge_server import services

    real_can_execute = services.can_execute

    def slow_can_execute(session, task_row, did):
        time.sleep(1.5)  # handler stalls long after the request was received
        return real_can_execute(session, task_row, did)

    monkeypatch.setattr("agentforge_server.app.can_execute", slow_can_execute)
    ingress = clock.server_now()
    claimed = claim_task(client, executor, task)
    finished = clock.server_now()
    assert finished - ingress >= 1.5

    claim = stored_claim(claimed["claim_id"])
    assert claim.received_at >= ingress
    assert claim.received_at < ingress + 1.0  # stamped at ingress, not at completion
    assert claim.lease_expires_at == pytest.approx(claim.received_at + CLAIM_LEASE_SECONDS, abs=1e-6)
    # A handler that ran 1.5s late did not gain 1.5s of lease.
    assert claim.lease_expires_at < finished + CLAIM_LEASE_SECONDS


def test_heartbeat_losing_the_lease_race_under_latency_is_rejected(client, monkeypatch):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claimed = claim_task(client, executor, task)
    with db.SessionLocal() as session:
        claim = session.get(Claim, claimed["claim_id"])
        claim.lease_expires_at = clock.server_now() + 0.4  # expires mid-handler
        session.commit()

    from agentforge_server import services

    real_guard = services.guard_active_claim

    def slow_guard(session, claim_row):
        time.sleep(1.0)
        return real_guard(session, claim_row)

    monkeypatch.setattr("agentforge_server.app.guard_active_claim", slow_guard)
    before = clock.server_now()
    response = signed_request(
        client, executor, "POST", f"/api/v1/claims/{claimed['claim_id']}/heartbeat", {},
        timestamp=time.time() + 58,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "claim is no longer active"
    with db.SessionLocal() as session:
        claim = session.get(Claim, claimed["claim_id"])
        # No extension happened, and none could have exceeded one lease anyway.
        assert claim.lease_expires_at < before + CLAIM_LEASE_SECONDS


def test_concurrent_heartbeats_never_accumulate_extra_lease(client):
    poster, executor = two_agents(client)
    task = create_task(client, poster)
    claimed = claim_task(client, executor, task)

    def heartbeat(offset):
        return signed_request(
            client, executor, "POST", f"/api/v1/claims/{claimed['claim_id']}/heartbeat", {},
            timestamp=time.time() + offset,
        )

    # Two concurrent writers, again matching SQLite's single-writer reality.
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(heartbeat, [58, -58]))
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    claim = stored_claim(claimed["claim_id"])
    assert claim.lease_expires_at == pytest.approx(claim.received_at + CLAIM_LEASE_SECONDS, abs=1e-6)
    assert claim.lease_expires_at <= clock.server_now() + CLAIM_LEASE_SECONDS + 1


def test_concurrent_stale_requests_are_all_rejected_without_state(client):
    poster, _ = two_agents(client)

    def stale(index):
        return signed_request(
            client, poster, "POST", "/api/v1/tasks", task_payload(),
            timestamp=time.time() - 120 - index,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(stale, range(8)))
    assert [response.status_code for response in responses] == [401] * 8
    assert {response.json()["detail"] for response in responses} == {
        "client clock drift exceeds tolerance"
    }
    with db.SessionLocal() as session:
        assert session.scalars(select(Task)).all() == []
        assert session.scalars(select(UsedNonce)).all() == []


# ---------------------------------------------------------------------------
# 6. SDK: an honest client with a skewed local clock self-corrects
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, headers, payload=None):
        self.headers = headers
        self.status_code = 200
        self.text = "{}"
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


def _client_with_canned_server_clock(server_time_header: str | None):
    from agentforge_sdk.client import AgentForgeClient

    identity = AgentIdentity.generate()
    client = AgentForgeClient("https://exchange.test", identity)
    seen: list[str] = []
    # Response headers the fake server sends back. Named distinctly from the
    # request headers so the two can never be confused.
    response_headers = (
        {} if server_time_header is None else {"X-Server-Timestamp": server_time_header}
    )

    class FakeHttp:
        # Signature mirrors httpx.Client.request, including the `headers`
        # keyword the SDK actually passes; the canned *response* headers come
        # from the enclosing scope under a different name.
        def request(self, method, url, content=None, headers=None):  # noqa: A002
            seen.append(headers["X-Agent-Timestamp"])
            return _FakeResponse(response_headers)

    client.http = FakeHttp()
    return client, seen


def test_sdk_learns_the_server_clock_and_signs_with_it():
    # The server clock runs two minutes ahead of this client's local clock, so
    # the first signed request would be outside the 60-second window and the
    # client must correct itself for the next one.
    client, seen = _client_with_canned_server_clock(repr(time.time() + 120))
    client.create_task({"kind": "research"})
    assert client.clock_offset == pytest.approx(120, abs=5)
    client.create_task({"kind": "research"})
    assert len(seen) == 2
    assert int(float(seen[1])) - int(float(seen[0])) == pytest.approx(120, abs=5)


@pytest.mark.parametrize("header", ["not-a-number", "", "nan", "inf", "-5", None])
def test_sdk_ignores_an_unusable_server_clock_header(header):
    client, seen = _client_with_canned_server_clock(header)
    client.create_task({"kind": "research"})
    client.create_task({"kind": "research"})
    assert client.clock_offset == 0.0
    assert len(seen) == 2
