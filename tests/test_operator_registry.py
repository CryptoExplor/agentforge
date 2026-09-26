"""Operator registry and role grants (Grok roadmap 1.1).

Covers peer-validation authorization in both modes:

- ``OPEN_OPERATORS=true`` (development): capability-declaring validators are
  self-granted the ``validator`` registry role at registration, and the
  check-time fallback keeps pre-existing dev agents working.
- ``OPEN_OPERATORS=false`` (production): only an explicit ``ACTIVE`` row in
  ``operator_role_grants`` authorizes a validation decision; everyone else
  receives 403 "agent not authorized as validator".
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.models import OperatorRoleGrant, ValidationDecision
from agentforge_server.operators import grant_role, revoke_role
from agentforge_server.settings import settings


@pytest.fixture(autouse=True)
def isolated_operator_policy(monkeypatch):
    """Each test starts from the development defaults with no operator grants."""
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    monkeypatch.setattr(settings, "open_operators", True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'operators.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


from helpers import register, signed_request  # noqa: E402  (single-source test helpers)


def decision_payload(identity: AgentIdentity, submission: dict) -> dict:
    decision_id = "VD_" + uuid.uuid4().hex
    core = {
        "decision_id": decision_id,
        "submission_id": submission["submission_id"],
        "validator_did": identity.did,
        "decision": "VERIFIED",
        "policy": "deterministic_then_domain",
        "checks": [{"code": "EVIDENCE_PRESENT", "result": "PASS"}],
        "reason_codes": ["ACCEPTANCE_CRITERIA_SATISFIED"],
        "settlement": {},
        "evidence_hash": submission["proof_hash"],
    }
    return {
        "decision_id": decision_id,
        "decision": "VERIFIED",
        "policy": "deterministic_then_domain",
        "checks": core["checks"],
        "reason_codes": core["reason_codes"],
        "settlement": {},
        "evidence_hash": submission["proof_hash"],
        "signature": identity.sign(canonical_json(core).encode()),
    }


def pending_submission(client: TestClient):
    """Drive poster -> executor -> submitted proof on a peer_review task."""
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    register(client, poster, {"name": "poster", "capabilities": ["research"], "chains": ["base"]})
    register(client, executor, {"name": "executor", "capabilities": ["proxy_security"], "chains": ["base"]})
    task_payload = {
        "kind": "expert",
        "visibility": "public",
        "origin": "external",
        "required_capabilities": ["proxy_security"],
        "chains": ["base"],
        "input": {"contract": "0xabc"},
        "acceptance": {"required_outputs": ["risk"], "required_evidence": ["bytecode_hash"]},
        "demand_provenance": {"type": "external_event", "level": 1, "source": "test", "source_ref": "event-1"},
        "generation_policy": {"economic_eligibility": "ECONOMIC_ELIGIBLE", "minimum_provenance_level": 1},
        "economics": {
            "mode": "BOUNTY",
            "reward": {"amount": "10", "asset": "MOCK"},
            "security_deposit": {"amount": "1", "asset": "MOCK"},
            "inference_budget": {"amount": "2", "asset": "MOCK"},
        },
        "deadline": time.time() + 3600,
    }
    created = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload)
    assert created.status_code == 200, created.text
    task = created.json()
    claimed = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert claimed.status_code == 200, claimed.text
    inference = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/inference",
        {"provider": "mock", "requested_compute": "1"},
    )
    assert inference.status_code == 200, inference.text
    session_id = inference.json()["session_id"]
    result = {"risk": "unknown"}
    evidence = [{"kind": "bytecode_hash", "content_hash": "sha256:abc"}]
    submission_id = "S_" + uuid.uuid4().hex
    created_at = time.time()
    proof_core = {
        "task_id": task["id"],
        "submission_id": submission_id,
        "executor_did": executor.did,
        "input_hash": sha256_json(task["input"]),
        "result_hash": sha256_json(result),
        "evidence": evidence,
        "inference_session_ids": [session_id],
        "created_at": created_at,
    }
    submitted = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions",
        {
            "submission_id": submission_id,
            "result": result,
            "evidence": evidence,
            "inference_session_ids": [session_id],
            "created_at": created_at,
            "proof_signature": executor.sign(canonical_json(proof_core).encode()),
        },
    )
    assert submitted.status_code == 200, submitted.text
    return poster, executor, task, submitted.json()


def grants(client: TestClient, did: str) -> list[OperatorRoleGrant]:
    with db.SessionLocal() as session:
        return list(
            session.scalars(select(OperatorRoleGrant).where(OperatorRoleGrant.agent_did == did)).all()
        )


def approve(identity: AgentIdentity):
    settings.trusted_validator_dids = settings.trusted_validator_dids | {identity.did}


# ---------------------------------------------------------------------------
# Open mode (development): self-registration keeps local suites working.
# ---------------------------------------------------------------------------


def test_open_mode_self_registration_grants_validator_role_and_allows_validation(client):
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)

    # Registration self-granted an explicit, auditable registry row.
    rows = grants(client, validator.did)
    assert len(rows) == 1
    assert (rows[0].role, rows[0].status, rows[0].granted_by) == ("validator", "ACTIVE", None)

    response = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "VERIFIED"
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "VERIFIED"


def test_open_mode_preexisting_capability_agent_remains_authorized(client):
    """The check-time fallback keeps dev agents registered before grants existed."""
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]})
    approve(validator)
    # Simulate a pre-registry dev agent: remove the self-grant row.
    with db.SessionLocal() as session:
        for row in session.scalars(select(OperatorRoleGrant)).all():
            session.delete(row)
        session.commit()

    response = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert response.status_code == 200, response.text


def test_registration_without_validator_capability_creates_no_grant(client):
    pending_submission(client)
    outsider = AgentIdentity.generate()
    register(client, outsider, {"name": "plain", "capabilities": ["research"], "chains": ["base"]})
    assert grants(client, outsider.did) == []


# ---------------------------------------------------------------------------
# Restricted mode (production): explicit grants only.
# ---------------------------------------------------------------------------


def test_restricted_mode_rejects_capability_only_validator_with_403(client, monkeypatch):
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    monkeypatch.setattr(settings, "open_operators", False)

    response = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "agent not authorized as validator"
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ValidationDecision)) == 0
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "SUBMITTED"


def test_restricted_mode_explicit_grant_authorizes_validation(client, monkeypatch):
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]})
    approve(validator)
    monkeypatch.setattr(settings, "open_operators", False)

    with db.SessionLocal() as session:
        grant = grant_role(session, did=validator.did, granted_by="did:key:operator", reason="roadmap 1.1")
        session.commit()
        assert grant.status == "ACTIVE"

    response = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert response.status_code == 200, response.text
    with db.SessionLocal() as session:
        decision = session.get(ValidationDecision, response.json()["decision_id"])
        assert decision is not None and decision.validator_did == validator.did


def test_revoked_grant_blocks_validation_and_reregistration_cannot_resurrect(client, monkeypatch):
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    monkeypatch.setattr(settings, "open_operators", False)

    with db.SessionLocal() as session:
        assert revoke_role(session, did=validator.did) is True
        session.commit()

    response = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "agent not authorized as validator"

    # Re-registering with the same capability must not resurrect the grant.
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    assert all(row.status == "REVOKED" for row in grants(client, validator.did))
    assert signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    ).status_code == 403


def test_restricted_mode_missing_grant_is_rejected_on_submission_scoped_endpoint(client, monkeypatch):
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    monkeypatch.setattr(settings, "open_operators", False)

    response = signed_request(
        client, validator, "POST", f"/api/v1/submissions/{submission['submission_id']}/validate",
        decision_payload(validator, submission),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "agent not authorized as validator"


def test_restricted_mode_dispute_resolution_requires_registry_role(client, monkeypatch):
    poster, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    monkeypatch.setattr(settings, "open_operators", False)

    opened = signed_request(client, poster, "POST", f"/api/v1/submissions/{submission['submission_id']}/disputes", {
        "dispute_id": "D_registry", "reason": "Please review this disputed submission",
    })
    assert opened.status_code == 200, opened.text
    response = signed_request(
        client, validator, "POST", "/api/v1/disputes/D_registry/resolve",
        decision_payload(validator, submission),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "agent not authorized as validator"


def test_validation_still_requires_operator_allowlist_and_capability(client):
    """D1 control is unchanged: allowlist and capability gates come first."""
    _, _, task, submission = pending_submission(client)
    unlisted = AgentIdentity.generate()
    register(client, unlisted, {"name": "unlisted", "capabilities": ["validator"], "chains": ["base"]})
    assert unlisted.did not in settings.trusted_validator_dids
    response = signed_request(
        client, unlisted, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(unlisted, submission),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "validator is not approved or validation-capable"

    # Allowlisted but capability-free agents are still rejected.
    incapable = AgentIdentity.generate()
    register(client, incapable, {"name": "incapable", "capabilities": [], "chains": ["base"]})
    approve(incapable)
    response = signed_request(
        client, incapable, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(incapable, submission),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "validator is not approved or validation-capable"


# ---------------------------------------------------------------------------
# Endpoint surface of the task-scoped validation route.
# ---------------------------------------------------------------------------


def test_task_validations_unknown_task_is_404(client):
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    missing = signed_request(client, validator, "POST", "/api/v1/tasks/T_missing/validations", {
        "decision_id": "VD_missing", "decision": "VERIFIED", "signature": "x" * 40,
    })
    assert missing.status_code == 404
    assert missing.json()["detail"] == "task not found"


def test_task_validations_without_pending_submission_is_409(client):
    """An authorized validator on a task with no submitted proof gets 409."""
    pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    poster = AgentIdentity.generate()
    register(client, poster, {"name": "idle-poster", "capabilities": ["research"], "chains": ["base"]})
    created = signed_request(client, poster, "POST", "/api/v1/tasks", {
        "kind": "research",
        "origin": "research",
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 1},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": {"mode": "REPUTATION"},
    })
    assert created.status_code == 200, created.text
    response = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{created.json()['id']}/validations",
        decision_payload(validator, {"submission_id": "S_none", "proof_hash": "x"}),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "task has no submission awaiting validation"


def test_task_validations_rejects_already_settled_submission(client):
    _, _, task, submission = pending_submission(client)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    approve(validator)
    first = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert first.status_code == 200, first.text
    second = signed_request(
        client, validator, "POST", f"/api/v1/tasks/{task['id']}/validations",
        decision_payload(validator, submission),
    )
    assert second.status_code == 409


def test_restricted_mode_registration_writes_no_grant_until_operator_grants(client, monkeypatch):
    monkeypatch.setattr(settings, "open_operators", False)
    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    assert grants(client, validator.did) == []
    # The operator grants the role directly in the registry.
    with db.SessionLocal() as session:
        grant = grant_role(session, did=validator.did, granted_by="did:key:operator")
        session.commit()
        assert grant.granted_by == "did:key:operator"


# ---------------------------------------------------------------------------
# Service-level and configuration guards.
# ---------------------------------------------------------------------------


def test_grant_role_is_idempotent_and_rejected_after_revocation(client):
    with db.SessionLocal() as session:
        did = "did:key:grant-idempotency"
        first = grant_role(session, did=did, granted_by="did:key:operator")
        second = grant_role(session, did=did, granted_by="did:key:other")
        assert first.id == second.id
        assert revoke_role(session, did=did) is True
        assert revoke_role(session, did=did) is False
        with pytest.raises(ValueError, match="revoked"):
            grant_role(session, did=did, granted_by="did:key:operator")
        # Grants must record an accountable operator DID.
        with pytest.raises(ValueError, match="granted_by"):
            grant_role(session, did=did, granted_by="  ")
        session.rollback()


def test_self_granted_rows_lose_authority_in_restricted_mode(client, monkeypatch):
    """A dev self-grant (granted_by NULL) is not an operator grant."""
    from agentforge_server.operators import active_grant, active_operator_grant, validator_role_active

    validator = AgentIdentity.generate()
    register(client, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]})
    monkeypatch.setattr(settings, "open_operators", False)
    with db.SessionLocal() as session:
        assert active_grant(session, validator.did) is not None  # self-grant row exists
        assert active_operator_grant(session, validator.did) is None  # but no operator grant
        assert validator_role_active(session, validator.did) is False
    # Back in open mode the same dev agent is authorized again (fallback).
    monkeypatch.setattr(settings, "open_operators", True)
    with db.SessionLocal() as session:
        assert validator_role_active(session, validator.did) is True


def test_production_configuration_refuses_open_operators(monkeypatch):
    from agentforge_server.admission import validate_security_configuration

    monkeypatch.setattr(settings, "environment", "production")
    # Hermetic against flag leaks from earlier suite members: only the flag
    # under test may distinguish the two runs below.
    monkeypatch.setattr(settings, "enable_mock_faucet", False)
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    monkeypatch.setattr(settings, "open_operators", True)
    with pytest.raises(ValueError, match="OPEN_OPERATORS"):
        validate_security_configuration()
    monkeypatch.setattr(settings, "open_operators", False)
    validate_security_configuration()
