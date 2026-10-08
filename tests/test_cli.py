"""Unified operator CLI coverage: every ``agentforge-cli`` command group
against the live in-process app.

The CLI's documented test seam is ``agentforge_cli.main.client_factory``: the
suite swaps it for a factory that points the SDK transport at the
``TestClient`` (the same injection ``tests/test_sdk.py`` uses), so every
command exercises the real ingress middleware, signature verification,
idempotency, deterministic auto-settlement and error mapping. Nothing is
mocked at the HTTP boundary, and no command is tested against a fabricated
response shape.

The long-running daemons are out of scope here on purpose: they remain
``scripts/agent_worker.py`` / ``scripts/validator_worker.py`` and keep their
own live-uvicorn suite (``tests/test_agent_worker.py``).
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from agentforge_cli import main as cli_main
from agentforge_sdk import AgentForgeClient, AgentIdentity
from agentforge_sdk.crypto import sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.settings import settings


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_operator_policy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    monkeypatch.setattr(settings, "open_operators", True)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A live app on a disposable SQLite database, with the mock faucet on."""
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'cli.db'}")
    with TestClient(create_app()) as http:
        yield http


@pytest.fixture
def run_cli(server, monkeypatch, capsys):
    """Point the CLI's client factory at the live app and run commands.

    Returns ``run(*argv) -> (exit_code, stdout, stderr)``.
    """

    def factory(base_url: str, identity: AgentIdentity, *, timeout: float = 30.0):
        client = AgentForgeClient(base_url, identity, timeout=timeout)
        client.transport.http.close()
        client.http = server
        # The CLI closes its client after every command; keep it from
        # closing the shared TestClient the whole suite runs on.
        client.close = lambda: None
        return client

    monkeypatch.setattr(cli_main, "client_factory", factory)

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli_main.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def flags(identity_path, *command: str) -> list[str]:
    """Common flags for one identity plus the command tail."""
    return [*command, "--base-url", "http://exchange.test", "--identity", str(identity_path)]


def approve_validator(identity: AgentIdentity) -> None:
    settings.trusted_validator_dids = settings.trusted_validator_dids | {identity.did}


def new_identity(run_cli, tmp_path, name: str) -> AgentIdentity:
    """Create an identity through the CLI itself (dogfooding ``identity new``)."""
    path = tmp_path / f"{name}.json"
    code, out, err = run_cli(*flags(path, "identity", "new", str(path)))
    assert code == 0, err
    identity = AgentIdentity.load(path)
    assert identity.did in out
    assert identity.private_key_hex not in out  # secrets never reach stdout
    return identity


def register(run_cli, identity: AgentIdentity, identity_path, name: str, capabilities: str) -> None:
    code, out, err = run_cli(
        *flags(identity_path, "register", "--name", name, "--capabilities", capabilities, "--chains", "base")
    )
    assert code == 0, err
    assert identity.did in out
    assert "status=active" in out


def task_body(*, strategy: str = "peer_review", acceptance: dict | None = None) -> dict:
    return {
        "kind": "expert",
        "visibility": "public",
        "origin": "external",
        "verification_strategy": strategy,
        "required_capabilities": ["proxy_security"],
        "chains": ["base"],
        "input": {"contract": "0xabc"},
        "acceptance": acceptance
        or {"required_outputs": ["risk"], "required_evidence": ["bytecode_hash"]},
        "demand_provenance": {
            "type": "external_event",
            "level": 1,
            "source": "test",
            "source_ref": "event-1",
        },
        "generation_policy": {
            "economic_eligibility": "ECONOMIC_ELIGIBLE",
            "minimum_provenance_level": 1,
        },
        "economics": {
            "mode": "BOUNTY",
            "reward": {"amount": "10", "asset": "MOCK"},
            "security_deposit": {"amount": "1", "asset": "MOCK"},
            "inference_budget": {"amount": "2", "asset": "MOCK"},
        },
        "deadline": time.time() + 3600,
    }


def create_task(run_cli, poster_path, tmp_path, body: dict) -> dict:
    task_file = tmp_path / f"task-{time.time_ns()}.json"
    task_file.write_text(json.dumps(body), encoding="utf-8")
    code, out, err = run_cli(*flags(poster_path, "tasks", "create", str(task_file), "--json"))
    assert code == 0, err
    task = json.loads(out)
    assert task["status"] == "FUNDED"
    return task


# ---------------------------------------------------------------------------
# Identity commands (no server needed)
# ---------------------------------------------------------------------------


def test_identity_new_show_and_overwrite_guard(tmp_path, run_cli):
    path = tmp_path / "agent.json"
    code, out, err = run_cli("identity", "new", str(path))
    assert code == 0, err
    identity = AgentIdentity.load(path)
    assert identity.did in out
    assert identity.private_key_hex not in out
    # mkstemp-based save: private from the first write.
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0

    code, out, err = run_cli("identity", "new", str(path))
    assert code == cli_main.EXIT_USAGE
    assert "refusing to overwrite" in err

    code, out, err = run_cli("identity", "new", str(path), "--force")
    assert code == 0, err
    replaced = AgentIdentity.load(path)
    assert replaced.did != identity.did  # a fresh key, not a rewrite

    code, out, err = run_cli("identity", "show", str(path))
    assert code == 0, err
    assert replaced.did in out
    assert replaced.private_key_hex not in out

    code, out, err = run_cli("identity", "show", str(path), "--json")
    assert code == 0, err
    assert json.loads(out)["did"] == replaced.did


def test_identity_show_warns_on_loose_permissions(tmp_path, run_cli):
    path = tmp_path / "loose.json"
    code, _, err = run_cli("identity", "new", str(path))
    assert code == 0, err
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

    code, out, _ = run_cli("identity", "show", str(path))
    assert code == 0
    assert "WARNING" in out and "chmod 600" in out


def test_identity_show_corrupt_file_is_usage_error(tmp_path, run_cli):
    path = tmp_path / "corrupt.json"
    path.write_text("invalid-json{", encoding="utf-8")
    code, _, err = run_cli("identity", "show", str(path))
    assert code == cli_main.EXIT_USAGE
    assert "cannot load identity" in err


def test_missing_identity_is_a_usage_error(tmp_path, run_cli):
    code, out, err = run_cli(
        "balance", "--base-url", "http://exchange.test", "--identity", str(tmp_path / "nope.json")
    )
    assert code == cli_main.EXIT_USAGE
    assert "identity file not found" in err
    assert "identity new" in err


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------


def test_health_is_public_and_json_parses(tmp_path, run_cli):
    missing = str(tmp_path / "no-identity.json")
    code, out, err = run_cli("health", "--base-url", "http://exchange.test", "--identity", missing)
    assert code == 0, err
    assert "status=ok" in out and "db_skew" in out

    code, out, err = run_cli("health", "--base-url", "http://exchange.test", "--identity", missing, "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert "clock" in payload


def test_env_defaults_are_picked_up(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_BASE_URL", "http://env.example:9999")
    monkeypatch.setenv("AGENTFORGE_IDENTITY", "/tmp/env-identity.json")
    args = cli_main.build_parser().parse_args(["health"])
    assert args.base_url == "http://env.example:9999"
    assert args.identity == "/tmp/env-identity.json"
    assert args.timeout == 30.0
    assert args.json is False


def test_version_and_help_exit_cleanly(capsys):
    with pytest.raises(SystemExit) as version_exit:
        cli_main.main(["--version"])
    assert version_exit.value.code == 0
    with pytest.raises(SystemExit) as help_exit:
        cli_main.main(["--help"])
    assert help_exit.value.code == 0
    assert "identity" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Deterministic strategy: one identity pair settles at submission time
# ---------------------------------------------------------------------------


def test_full_deterministic_flow(tmp_path, run_cli):
    poster = new_identity(run_cli, tmp_path, "poster")
    worker = new_identity(run_cli, tmp_path, "worker")
    register(run_cli, poster, tmp_path / "poster.json", "poster", "research")
    register(run_cli, worker, tmp_path / "worker.json", "worker", "proxy_security")

    result = {"risk": "unknown"}
    body = task_body(
        strategy="deterministic",
        acceptance={
            "required_outputs": ["risk"],
            "required_evidence": ["bytecode_hash"],
            "expected_result_hash": sha256_json(result),
        },
    )
    task = create_task(run_cli, tmp_path / "poster.json", tmp_path, body)

    # Public discovery sees the funded task.
    code, out, err = run_cli(
        *flags(tmp_path / "poster.json", "tasks", "list", "--status", "FUNDED", "--json")
    )
    assert code == 0, err
    assert any(item["id"] == task["id"] for item in json.loads(out)["tasks"])

    # Claim, then submit the committed result through file + inline evidence.
    code, out, err = run_cli(*flags(tmp_path / "worker.json", "claim", task["id"]))
    assert code == 0, err
    assert "claim=" in out

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps(result), encoding="utf-8")
    code, out, err = run_cli(
        *flags(
            tmp_path / "worker.json",
            "submit",
            task["id"],
            "--result",
            str(result_file),
            "--evidence",
            '[{"kind": "bytecode_hash", "content_hash": "sha256:abc"}]',
            "--json",
        )
    )
    assert code == 0, err
    submission = json.loads(out)
    assert submission["status"] == "VERIFIED"  # deterministic auto-settlement

    # Task, escrow and balances settled atomically in that same transaction:
    # 10 reward + unused 2 budget + 1 deposit.
    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "get", task["id"], "--json"))
    assert code == 0, err
    settled = json.loads(out)
    assert settled["status"] == "VERIFIED"
    assert settled["escrow"]["status"] == "RELEASED"

    code, out, err = run_cli(*flags(tmp_path / "worker.json", "balance", "--json"))
    assert code == 0, err
    assert json.loads(out)["balance"] == "1010"

    code, out, err = run_cli(*flags(tmp_path / "poster.json", "balance"))
    assert code == 0, err
    assert "MOCK 990" in out

    # Reputation and the worker's own audit feed reflect the settlement.
    code, out, err = run_cli(
        *flags(tmp_path / "poster.json", "reputation", worker.did, "--json")
    )
    assert code == 0, err
    assert json.loads(out)["overall"] == 1.0

    code, out, err = run_cli(*flags(tmp_path / "worker.json", "events"))
    assert code == 0, err
    assert "TASK_CLAIMED" in out


# ---------------------------------------------------------------------------
# Peer review: discovery index, task-scoped verify, submission-scoped reject
# ---------------------------------------------------------------------------


def submitted_peer_review_task(run_cli, tmp_path):
    poster = new_identity(run_cli, tmp_path, "poster")
    executor = new_identity(run_cli, tmp_path, "executor")
    register(run_cli, poster, tmp_path / "poster.json", "poster", "research")
    register(run_cli, executor, tmp_path / "executor.json", "executor", "proxy_security")
    task = create_task(run_cli, tmp_path / "poster.json", tmp_path, task_body())
    code, _, err = run_cli(*flags(tmp_path / "executor.json", "claim", task["id"]))
    assert code == 0, err
    code, out, err = run_cli(
        *flags(
            tmp_path / "executor.json",
            "submit",
            task["id"],
            "--result",
            '{"risk": "unknown"}',
            "--evidence",
            '[{"kind": "bytecode_hash", "content_hash": "sha256:abc"}]',
            "--json",
        )
    )
    assert code == 0, err
    submission = json.loads(out)
    assert submission["status"] == "SUBMITTED"
    return task, submission


def test_peer_review_validate_task_settles(tmp_path, run_cli):
    task, submission = submitted_peer_review_task(run_cli, tmp_path)
    validator = new_identity(run_cli, tmp_path, "validator")
    register(run_cli, validator, tmp_path / "validator.json", "validator", "validation")
    approve_validator(validator)

    # The discovery index is how a third party learns the submission id.
    code, out, err = run_cli(
        *flags(tmp_path / "validator.json", "tasks", "submissions", task["id"], "--json")
    )
    assert code == 0, err
    rows = json.loads(out)["submissions"]
    assert rows[0]["submission_id"] == submission["submission_id"]

    code, out, err = run_cli(
        *flags(
            tmp_path / "validator.json",
            "validate-task",
            task["id"],
            "--submission-id",
            submission["submission_id"],
            "--decision",
            "VERIFIED",
            "--reason-codes",
            "ACCEPTANCE_CRITERIA_SATISFIED",
        )
    )
    assert code == 0, err
    assert "decision recorded" in out and "VERIFIED" in out

    code, out, err = run_cli(*flags(tmp_path / "validator.json", "tasks", "get", task["id"], "--json"))
    assert code == 0, err
    settled = json.loads(out)
    assert settled["status"] == "VERIFIED"
    assert settled["escrow"]["status"] == "RELEASED"

    # The submission and proof reads stay available after settlement.
    code, out, err = run_cli(
        *flags(tmp_path / "validator.json", "submission", submission["submission_id"])
    )
    assert code == 0, err
    assert submission["proof_hash"] in out or "proof_hash" in out
    code, out, err = run_cli(
        *flags(tmp_path / "validator.json", "proof", submission["submission_id"], "--json")
    )
    assert code == 0, err
    assert json.loads(out)["proof_hash"] == submission["proof_hash"]


def test_submission_scoped_rejection_refunds(tmp_path, run_cli):
    task, submission = submitted_peer_review_task(run_cli, tmp_path)
    validator = new_identity(run_cli, tmp_path, "validator")
    register(run_cli, validator, tmp_path / "validator.json", "validator", "validation")
    approve_validator(validator)

    code, out, err = run_cli(
        *flags(
            tmp_path / "validator.json",
            "validate",
            submission["submission_id"],
            "--decision",
            "REJECTED",
            "--reason-codes",
            "ACCEPTANCE_CRITERIA_UNSATISFIED",
            "--json",
        )
    )
    assert code == 0, err
    assert json.loads(out)["decision"] == "REJECTED"

    code, out, err = run_cli(*flags(tmp_path / "validator.json", "tasks", "get", task["id"], "--json"))
    assert code == 0, err
    settled = json.loads(out)
    assert settled["status"] == "REJECTED"
    assert settled["escrow"]["status"] == "REFUNDED"


def test_dispute_open_and_resolve(tmp_path, run_cli):
    task, submission = submitted_peer_review_task(run_cli, tmp_path)
    validator = new_identity(run_cli, tmp_path, "validator")
    register(run_cli, validator, tmp_path / "validator.json", "validator", "validation")
    approve_validator(validator)

    code, out, err = run_cli(
        *flags(
            tmp_path / "poster.json",
            "dispute",
            "open",
            submission["submission_id"],
            "--reason",
            "The acceptance interpretation needs an independent review.",
            "--json",
        )
    )
    assert code == 0, err
    dispute = json.loads(out)

    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "get", task["id"], "--json"))
    assert code == 0, err
    assert json.loads(out)["status"] == "DISPUTED"

    code, out, err = run_cli(
        *flags(
            tmp_path / "validator.json",
            "dispute",
            "resolve",
            dispute["dispute_id"],
            "--submission-id",
            submission["submission_id"],
            "--decision",
            "REJECTED",
            "--json",
        )
    )
    assert code == 0, err
    assert json.loads(out)["decision"] == "REJECTED"

    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "get", task["id"], "--json"))
    assert code == 0, err
    assert json.loads(out)["status"] == "REJECTED"


# ---------------------------------------------------------------------------
# Cancellation, error mapping and input validation
# ---------------------------------------------------------------------------


def test_cancel_and_error_mapping(tmp_path, run_cli):
    poster = new_identity(run_cli, tmp_path, "poster")
    outsider = new_identity(run_cli, tmp_path, "outsider")
    register(run_cli, poster, tmp_path / "poster.json", "poster", "research")
    register(run_cli, outsider, tmp_path / "outsider.json", "outsider", "research")
    task = create_task(run_cli, tmp_path / "poster.json", tmp_path, task_body())

    # A non-owner cannot cancel: the CLI surfaces the live 403.
    code, out, err = run_cli(*flags(tmp_path / "outsider.json", "tasks", "cancel", task["id"]))
    assert code == cli_main.EXIT_FAILURE
    assert "HTTP 403" in err

    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "cancel", task["id"]))
    assert code == 0, err
    assert "CANCELLED" in out

    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "get", "T_missing"))
    assert code == cli_main.EXIT_FAILURE
    assert "HTTP 404" in err


def test_task_create_input_validation(tmp_path, run_cli):
    poster = new_identity(run_cli, tmp_path, "poster")
    register(run_cli, poster, tmp_path / "poster.json", "poster", "research")

    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "create", str(tmp_path / "absent.json")))
    assert code == cli_main.EXIT_USAGE
    assert "neither an existing file nor inline JSON" in err

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "create", str(bad)))
    assert code == cli_main.EXIT_USAGE
    assert "invalid JSON" in err

    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "create", str(array)))
    assert code == cli_main.EXIT_USAGE
    assert "expected a JSON object" in err

    # A syntactically valid but server-rejected body maps to the live error.
    rejected = tmp_path / "rejected.json"
    rejected.write_text(json.dumps({"kind": "expert"}), encoding="utf-8")
    code, out, err = run_cli(*flags(tmp_path / "poster.json", "tasks", "create", str(rejected)))
    assert code == cli_main.EXIT_FAILURE
    assert "HTTP 4" in err


def test_submit_result_validation(tmp_path, run_cli):
    task, _ = submitted_peer_review_task(run_cli, tmp_path)
    code, out, err = run_cli(
        *flags(
            tmp_path / "executor.json",
            "submit",
            task["id"],
            "--result",
            str(tmp_path / "missing.json"),
        )
    )
    assert code == cli_main.EXIT_USAGE
    assert "--result" in err


# ---------------------------------------------------------------------------
# Discovery filters and pagination through the CLI
# ---------------------------------------------------------------------------


def test_tasks_list_filters_and_pagination(tmp_path, run_cli):
    poster = new_identity(run_cli, tmp_path, "poster")
    register(run_cli, poster, tmp_path / "poster.json", "poster", "research")
    for _ in range(3):
        create_task(run_cli, tmp_path / "poster.json", tmp_path, task_body())

    code, out, err = run_cli(
        *flags(
            tmp_path / "poster.json",
            "tasks",
            "list",
            "--status",
            "FUNDED",
            "--capability",
            "proxy_security",
            "--limit",
            "2",
            "--offset",
            "0",  # pagination metadata is only returned for cursor/offset calls
            "--json",
        )
    )
    assert code == 0, err
    page = json.loads(out)
    assert len(page["tasks"]) == 2
    assert page["has_more"] is True
    assert page["next_cursor"]

    code, out, err = run_cli(
        *flags(
            tmp_path / "poster.json",
            "tasks",
            "list",
            "--limit",
            "2",
            "--cursor",
            page["next_cursor"],
        )
    )
    assert code == 0, err
    # Human rendering carries the pagination footer; keyset totals are
    # measured from the seek position, so only the shape is asserted.
    assert "(total=" in out and "has_more=False" in out
    first_ids = {task["id"] for task in page["tasks"]}
    code, out, err = run_cli(
        *flags(
            tmp_path / "poster.json",
            "tasks",
            "list",
            "--limit",
            "2",
            "--cursor",
            page["next_cursor"],
            "--json",
        )
    )
    second = json.loads(out)["tasks"]
    assert len(second) == 1
    assert not first_ids & {task["id"] for task in second}

    # The capability index and agent search are public reads via the CLI.
    # Only the poster registered in this scenario, so only its capability
    # appears in the index.
    code, out, err = run_cli(*flags(tmp_path / "poster.json", "capabilities"))
    assert code == 0, err
    assert "research: agents=1" in out

    code, out, err = run_cli(
        *flags(tmp_path / "poster.json", "agents", "search", "--capability", "research", "--json")
    )
    assert code == 0, err
    assert [agent["did"] for agent in json.loads(out)["agents"]] == [poster.did]
