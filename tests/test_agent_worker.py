"""The autonomous daemons, driven against a real TCP-served app.

These are deliberately not mocked. ``scripts/agent_worker.py`` and
``scripts/validator_worker.py`` are long-running processes whose whole value is
in behaviour the request/response tests cannot see -- the lease heartbeat that
runs *while* a handler is working, the refusal to claim work the agent cannot
finish, the peer-review discovery path, and shutting down without abandoning a
claim -- so every test here runs them against uvicorn over real HTTP, and the
signal test runs the actual CLI as a separate process.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from agentforge_sdk import AgentForgeClient, AgentIdentity
from agentforge_sdk.crypto import sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.settings import settings

# pytest's import roots are server/ and sdk/python/, not the repository root
# where the standalone operator scripts live.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.agent_worker import (  # noqa: E402
    AgentWorker,
    WorkerConfig,
    capability_names,
    demo_marketplace_handler,
)
from scripts.simulate_marketplace import task_payload  # noqa: E402
from scripts.validator_worker import (  # noqa: E402
    ValidatorConfig,
    ValidatorWorker,
    evaluate,
)

DEMO_RESULT = {"answer": "ready"}


# ---------------------------------------------------------------------------
# Live server fixture
# ---------------------------------------------------------------------------


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A real uvicorn instance on a disposable SQLite database."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    monkeypatch.setattr(settings, "registration_open", True)
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "open_operators", True)
    db.configure_database(f"sqlite:///{tmp_path / 'worker.db'}")

    port = unused_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise AssertionError("uvicorn did not become healthy")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def registered(base_url: str, identity: AgentIdentity, manifest: dict) -> AgentForgeClient:
    client = AgentForgeClient(base_url, identity, timeout=5)
    client.register(manifest)
    return client


def post_task(poster: AgentForgeClient, *, strategy: str, result: dict | None = None) -> dict:
    task = poster.create_task(task_payload(result if result is not None else DEMO_RESULT, strategy))
    assert task["status"] == "FUNDED"
    return task


def run_in_thread(worker) -> threading.Thread:
    thread = threading.Thread(target=worker.run, name="worker-under-test", daemon=True)
    thread.start()
    return thread


def wait_until(predicate, *, timeout: float = 25.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def worker_config(base_url: str, **overrides) -> WorkerConfig:
    defaults = dict(
        base_url=base_url,
        capabilities=("marketplace_demo",),
        poll_interval=0.2,
        max_poll_interval=1.0,
        jitter_ratio=0.0,
        timeout=5.0,
        heartbeat_interval=0.15,
        shutdown_grace=20.0,
        max_tasks=1,
    )
    defaults.update(overrides)
    return WorkerConfig(**defaults)


# ---------------------------------------------------------------------------
# Executor daemon
# ---------------------------------------------------------------------------


def test_worker_claims_heartbeats_and_submits_against_live_uvicorn(live_server):
    """The full autonomous lifecycle, including heartbeats during execution."""
    poster = registered(
        live_server, AgentIdentity.generate(), {"name": "poster", "capabilities": ["research"], "chains": ["local"]}
    )
    executor_identity = AgentIdentity.generate()
    registered(
        live_server,
        executor_identity,
        {"name": "executor", "capabilities": ["marketplace_demo"], "chains": ["local"]},
    )
    task = post_task(poster, strategy="deterministic")

    def slow_handler(task_view, client):
        # Long enough that the lease must be heartbeated mid-execution.
        time.sleep(0.6)
        return demo_marketplace_handler(task_view, client)

    worker = AgentWorker(
        executor_identity,
        worker_config(live_server),
        handlers={"marketplace_demo": slow_handler},
    )
    thread = run_in_thread(worker)
    try:
        assert wait_until(lambda: worker.stats.submissions >= 1), worker.stats.as_dict()
    finally:
        worker.request_stop()
        thread.join(timeout=25)

    assert not thread.is_alive()
    stats = worker.stats
    assert stats.claims_won == 1
    assert stats.submissions == 1
    assert stats.verified == 1, "deterministic task should settle at submission time"
    assert stats.heartbeats >= 1, "the lease must be heartbeated while the handler runs"
    assert stats.handler_errors == 0
    assert stats.leases_lost == 0

    settled = poster.get_task(task["id"])
    assert settled["status"] == "VERIFIED"
    assert settled["escrow"]["status"] == "RELEASED"
    assert settled["escrow"]["released_amount"] == "9.5"

    executor_client = AgentForgeClient(live_server, executor_identity, timeout=5)
    try:
        assert executor_client.balance()["balance"] == "1009.5"
        assert executor_client.reputation()["overall"] == 1.0
    finally:
        executor_client.close()
    poster.close()


def test_worker_refuses_work_it_cannot_finish(live_server):
    """No handler or an undeclared capability means no claim at all.

    Claiming blind would be worse than useless: the server rejects the claim
    with 403 for a missing capability, and an abandoned lease costs executor
    reputation when the reaper expires it.
    """
    poster = registered(
        live_server, AgentIdentity.generate(), {"name": "poster", "capabilities": ["research"], "chains": ["local"]}
    )
    executor_identity = AgentIdentity.generate()
    registered(
        live_server,
        executor_identity,
        {"name": "executor", "capabilities": ["marketplace_demo"], "chains": ["local"]},
    )

    payload = task_payload(DEMO_RESULT, "deterministic")
    payload["required_capabilities"] = ["quantum_haruspicy"]
    task = poster.create_task(payload)

    worker = AgentWorker(
        executor_identity,
        worker_config(live_server, once=True, max_tasks=None),
        handlers={"marketplace_demo": demo_marketplace_handler},
    )
    worker.run()

    assert worker.stats.claims_attempted == 0
    assert worker.stats.claims_won == 0
    assert poster.get_task(task["id"])["status"] == "FUNDED"
    poster.close()


def test_worker_withholds_a_proof_when_the_handler_fails(live_server):
    """A failing handler must not submit a proof it knows is wrong."""
    poster = registered(
        live_server, AgentIdentity.generate(), {"name": "poster", "capabilities": ["research"], "chains": ["local"]}
    )
    executor_identity = AgentIdentity.generate()
    registered(
        live_server,
        executor_identity,
        {"name": "executor", "capabilities": ["marketplace_demo"], "chains": ["local"]},
    )
    task = post_task(poster, strategy="deterministic")

    def broken_handler(task_view, client):
        raise RuntimeError("upstream model unavailable")

    worker = AgentWorker(
        executor_identity,
        worker_config(live_server, once=True),
        handlers={"marketplace_demo": broken_handler},
    )
    worker.run()

    assert worker.stats.claims_won == 1
    assert worker.stats.handler_errors == 1
    assert worker.stats.submissions == 0
    # The claim stands until its lease expires; no proof was invented for it.
    assert poster.get_task(task["id"])["status"] == "CLAIMED"
    poster.close()


def test_worker_stops_retrying_a_permanently_refused_task(live_server):
    """A 403 is a property of this pair, not a passing condition.

    Regression for a live-run finding: before the declined map the poll loop
    re-attempted an independence-refused claim on every cycle (51 attempts,
    50 refusals in one short run) instead of leaving it alone.
    """
    poster_identity = AgentIdentity.generate()
    executor_identity = AgentIdentity.generate()
    poster = registered(
        live_server,
        poster_identity,
        {
            "name": "poster",
            "capabilities": ["research"],
            "chains": ["local"],
            "operator_group": "shared-operator",
        },
    )
    registered(
        live_server,
        executor_identity,
        {
            "name": "executor",
            "capabilities": ["marketplace_demo"],
            "chains": ["local"],
            # Same operator group as the poster: the server refuses the claim
            # with 403 "executor is not independent" every single time.
            "operator_group": "shared-operator",
        },
    )
    task = post_task(poster, strategy="deterministic")

    worker = AgentWorker(
        executor_identity,
        worker_config(live_server, max_tasks=None),
        handlers={"marketplace_demo": demo_marketplace_handler},
    )
    thread = run_in_thread(worker)
    try:
        assert wait_until(lambda: worker.stats.claims_declined >= 1), worker.stats.as_dict()
        # Several more poll cycles must not produce more claim attempts.
        polls_then = worker.stats.polls
        assert wait_until(lambda: worker.stats.polls >= polls_then + 3, timeout=10)
    finally:
        worker.request_stop()
        thread.join(timeout=25)

    assert not thread.is_alive()
    assert worker.stats.claims_attempted == 1, "the refused task must not be retried"
    assert worker.stats.claims_declined == 1
    assert worker.stats.claims_won == 0
    assert worker.declined(task["id"]) is True
    assert poster.get_task(task["id"])["status"] == "FUNDED"
    poster.close()


def test_declined_map_is_bounded():
    """A daemon that runs for weeks must not grow the refusal map forever."""
    from scripts.agent_worker import MAX_DECLINED_TASKS

    worker = AgentWorker(AgentIdentity.generate(), WorkerConfig(base_url="http://unused"))
    for index in range(MAX_DECLINED_TASKS + 25):
        worker.decline(f"T_{index}", "403 refused")
    assert len(worker._declined) == MAX_DECLINED_TASKS
    # Oldest entries were evicted; the most recent were kept.
    assert worker.declined("T_0") is False
    assert worker.declined(f"T_{MAX_DECLINED_TASKS + 24}") is True


def test_capability_selection_matches_the_server_rule():
    """A task is workable only when the worker declares *every* capability."""
    config = WorkerConfig(base_url="http://unused", capabilities=("alpha", "beta"))
    worker = AgentWorker(
        AgentIdentity.generate(),
        config,
        handlers={"alpha": demo_marketplace_handler},
    )
    assert worker.handler_for({"required_capabilities": ["alpha"]}) is not None
    # Declared, but the worker has no handler registered for it.
    assert worker.handler_for({"required_capabilities": ["beta"]}) is None
    # Server-side ``can_execute`` requires the full set, so neither do we.
    assert worker.handler_for({"required_capabilities": ["alpha", "gamma"]}) is None
    assert capability_names({"required_capabilities": ["alpha", {"name": "beta"}]}) == {"alpha", "beta"}


def test_worker_cli_exits_cleanly_on_sigterm(live_server, tmp_path):
    """The real CLI, stopped the way systemd stops it."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / "server"), str(REPO_ROOT / "sdk" / "python")]
    )
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "agent_worker.py"),
            "--base-url",
            live_server,
            "--identity-path",
            str(tmp_path / "worker-identity.json"),
            "--capabilities",
            "marketplace_demo",
            "--poll-interval",
            "0.3",
            "--log-level",
            "WARNING",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
    )
    try:
        # Let it register and complete at least one poll cycle.
        time.sleep(3)
        assert process.poll() is None, "worker exited before it was signalled"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung worker
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode == 0, f"stdout={stdout!r} stderr={stderr!r}"
    report = json.loads(stdout)
    assert report["did"].startswith("did:key:z")
    assert report["stats"]["polls"] >= 1
    # Identity persisted, and private from its first write.
    identity_file = tmp_path / "worker-identity.json"
    assert identity_file.exists()
    assert oct(identity_file.stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# Validator daemon
# ---------------------------------------------------------------------------


def test_validator_worker_verifies_a_peer_reviewed_proof(live_server, monkeypatch):
    """Discover the pending proof, re-derive acceptance, decide, settle."""
    poster_identity = AgentIdentity.generate()
    executor_identity = AgentIdentity.generate()
    validator_identity = AgentIdentity.generate()
    monkeypatch.setattr(
        settings, "trusted_validator_dids", frozenset({validator_identity.did})
    )

    poster = registered(
        live_server, poster_identity, {"name": "poster", "capabilities": ["research"], "chains": ["local"]}
    )
    registered(
        live_server,
        executor_identity,
        {"name": "executor", "capabilities": ["marketplace_demo"], "chains": ["local"]},
    )
    registered(
        live_server,
        validator_identity,
        {"name": "validator", "capabilities": ["validation"], "chains": ["local"]},
    )
    task = post_task(poster, strategy="peer_review")

    executor = AgentWorker(
        executor_identity,
        worker_config(live_server, once=True),
        handlers={"marketplace_demo": demo_marketplace_handler},
    )
    executor.run()
    assert executor.stats.submissions == 1
    assert poster.get_task(task["id"])["status"] == "SUBMITTED"

    validator = ValidatorWorker(
        validator_identity,
        ValidatorConfig(
            base_url=live_server, poll_interval=0.2, jitter_ratio=0.0, timeout=5.0, once=True
        ),
    )
    validator.run()

    assert validator.stats.reviewed == 1
    assert validator.stats.verified == 1
    assert validator.stats.withheld == 0

    settled = poster.get_task(task["id"])
    assert settled["status"] == "VERIFIED"
    assert settled["escrow"]["status"] == "RELEASED"
    assert settled["escrow"]["released_amount"] == "9.5"
    poster.close()


def test_validator_withholds_a_decision_when_acceptance_fails(live_server, monkeypatch):
    """A failing proof is reported, not auto-slashed, unless --reject is set."""
    poster_identity = AgentIdentity.generate()
    executor_identity = AgentIdentity.generate()
    validator_identity = AgentIdentity.generate()
    monkeypatch.setattr(
        settings, "trusted_validator_dids", frozenset({validator_identity.did})
    )

    poster = registered(
        live_server, poster_identity, {"name": "poster", "capabilities": ["research"], "chains": ["local"]}
    )
    registered(
        live_server,
        executor_identity,
        {"name": "executor", "capabilities": ["marketplace_demo"], "chains": ["local"]},
    )
    registered(
        live_server,
        validator_identity,
        {"name": "validator", "capabilities": ["validation"], "chains": ["local"]},
    )
    task = post_task(poster, strategy="peer_review")

    def wrong_handler(task_view, client):
        return {
            "result": {"answer": "not-ready"},
            "evidence": [{"kind": "demo_receipt", "content_hash": "sha256:wrong"}],
        }

    executor = AgentWorker(
        executor_identity,
        worker_config(live_server, once=True),
        handlers={"marketplace_demo": wrong_handler},
    )
    executor.run()
    assert executor.stats.submissions == 1

    validator = ValidatorWorker(
        validator_identity,
        ValidatorConfig(
            base_url=live_server, poll_interval=0.2, jitter_ratio=0.0, timeout=5.0, once=True
        ),
    )
    validator.run()

    assert validator.stats.reviewed == 1
    assert validator.stats.withheld == 1
    assert validator.stats.verified == 0
    assert validator.stats.rejected == 0
    # Escrow is untouched: withholding is not a settlement.
    still_pending = poster.get_task(task["id"])
    assert still_pending["status"] == "SUBMITTED"
    assert still_pending["escrow"]["status"] == "FUNDED"
    poster.close()


def test_validator_acceptance_evaluation_is_independent():
    """``evaluate`` derives its verdict from the task and proof alone."""
    task = {
        "acceptance": {
            "required_outputs": ["answer"],
            "required_evidence": ["demo_receipt"],
            "expected_result_hash": sha256_json(DEMO_RESULT),
            "result_schema": {
                "type": "object",
                "properties": {"answer": {"const": "ready"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
    }
    good = {
        "result": DEMO_RESULT,
        "result_hash": sha256_json(DEMO_RESULT),
        "evidence": [{"kind": "demo_receipt", "content_hash": "sha256:x"}],
    }
    passed, checks, reasons = evaluate(task, good)
    assert passed is True
    assert reasons == ["ACCEPTANCE_CRITERIA_SATISFIED"]
    assert {item["kind"] for item in checks} >= {
        "RESULT_HASH_MATCH",
        "EXPECTED_RESULT_HASH_MATCH",
        "REQUIRED_OUTPUTS_PRESENT",
        "REQUIRED_EVIDENCE_PRESENT",
        "RESULT_SCHEMA_VALID",
    }

    wrong_result = {"answer": "nope"}
    bad = {
        "result": wrong_result,
        "result_hash": sha256_json(wrong_result),
        "evidence": [],
    }
    passed, _checks, reasons = evaluate(task, bad)
    assert passed is False
    assert "EXPECTED_RESULT_HASH_MISMATCH" in reasons
    assert "REQUIRED_EVIDENCE_MISSING" in reasons
    assert "RESULT_SCHEMA_VIOLATION" in reasons

    # A result body that does not reproduce its own stored hash stops there.
    tampered = {"result": DEMO_RESULT, "result_hash": "sha256:not-the-hash", "evidence": []}
    passed, checks, reasons = evaluate(task, tampered)
    assert passed is False
    assert reasons == ["RESULT_HASH_MISMATCH"]
    assert [item["kind"] for item in checks] == ["RESULT_HASH_MATCH"]
