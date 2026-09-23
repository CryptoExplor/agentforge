from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes, sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.models import Claim, Task
from agentforge_server.settings import settings

from test_audit_fixes import (
    decision_payload,
    register,
    signed_request,
    submit,
    task_payload,
)


def setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'dispute.db'}")
    return TestClient(create_app())


def test_dispute_open_and_resolution_are_idempotent(tmp_path, monkeypatch):
    with setup_client(tmp_path, monkeypatch) as client:
        poster = AgentIdentity.generate()
        executor = AgentIdentity.generate()
        validator = AgentIdentity.generate()
        register(client, poster, {"name": "poster", "capabilities": [], "chains": ["base"]})
        register(client, executor, {"name": "executor", "capabilities": [], "chains": ["base"]})
        register(client, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]})
        # Explicit operator grant; registration metadata itself conveys no authority.
        settings.trusted_validator_dids = settings.trusted_validator_dids | {validator.did}
        task_response = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload(economics={
            "mode": "BOUNTY",
            "reward": {"amount": "10", "asset": "MOCK"},
            "security_deposit": {"amount": "2", "asset": "MOCK"},
            "inference_budget": {"amount": "1", "asset": "MOCK"},
        }))
        assert task_response.status_code == 200
        task = task_response.json()
        assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
        submission, _ = submit(client, task, executor)

        dispute_body = {
            "dispute_id": "D_" + uuid.uuid4().hex,
            "reason": "The acceptance interpretation needs an independent review.",
            "additional_evidence": [],
        }
        opened = signed_request(
            client, poster, "POST", f"/api/v1/submissions/{submission['submission_id']}/disputes", dispute_body, key="dispute-key"
        )
        replay = signed_request(
            client, poster, "POST", f"/api/v1/submissions/{submission['submission_id']}/disputes", dispute_body, key="dispute-key"
        )
        assert opened.status_code == replay.status_code == 200
        assert opened.json() == replay.json()
        assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "DISPUTED"

        decision = decision_payload(client, validator, submission, decision="REJECTED")
        resolved = signed_request(
            client, validator, "POST", f"/api/v1/disputes/{opened.json()['dispute_id']}/resolve", decision, key="resolve-key"
        )
        resolved_replay = signed_request(
            client, validator, "POST", f"/api/v1/disputes/{opened.json()['dispute_id']}/resolve", decision, key="resolve-key"
        )
        assert resolved.status_code == resolved_replay.status_code == 200
        assert resolved.json() == resolved_replay.json()
        assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "REJECTED"


def test_server_side_group_independence_blocks_executor_and_validator(tmp_path, monkeypatch):
    with setup_client(tmp_path, monkeypatch) as client:
        poster = AgentIdentity.generate()
        same_operator_executor = AgentIdentity.generate()
        independent_executor = AgentIdentity.generate()
        same_operator_validator = AgentIdentity.generate()
        register(client, poster, {"name": "poster", "capabilities": [], "operator_group": "op-a", "chains": ["base"]})
        register(client, same_operator_executor, {"name": "same", "capabilities": [], "operator_group": "op-a", "chains": ["base"]})
        register(client, independent_executor, {"name": "executor", "capabilities": [], "operator_group": "op-b", "chains": ["base"]})
        register(client, same_operator_validator, {"name": "validator", "capabilities": ["validation"], "operator_group": "op-a", "chains": ["base"]})
        # Explicit operator grant; registration metadata itself conveys no authority.
        settings.trusted_validator_dids = settings.trusted_validator_dids | {same_operator_validator.did}
        task_response = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload())
        task = task_response.json()
        blocked = signed_request(client, same_operator_executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
        assert blocked.status_code == 403
        claimed = signed_request(client, independent_executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
        assert claimed.status_code == 200
        submission, _ = submit(client, task, independent_executor)
        decision = decision_payload(client, same_operator_validator, submission)
        blocked_validation = signed_request(
            client,
            same_operator_validator,
            "POST",
            f"/api/v1/submissions/{submission['submission_id']}/validate",
            decision,
        )
        assert blocked_validation.status_code == 403


def test_ancestry_and_reciprocal_history_are_server_derived(tmp_path, monkeypatch):
    # Exercise the service's derived relationship fields without fabricating
    # client-provided anti-circularity data.
    with setup_client(tmp_path, monkeypatch) as client:
        poster = AgentIdentity.generate()
        executor = AgentIdentity.generate()
        register(client, poster, {"name": "poster", "capabilities": [], "chains": ["base"]})
        register(client, executor, {"name": "executor", "capabilities": [], "chains": ["base"]})
        parent = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload())
        assert parent.status_code == 200
        child_body = task_payload()
        child_body["parent_task_id"] = parent.json()["id"]
        child = signed_request(client, poster, "POST", "/api/v1/tasks", child_body)
        assert child.status_code == 200
        anti = child.json()["anti_circularity"]
        assert anti["ancestry"] == [parent.json()["id"]]
        assert anti["ancestry_posters"] == [poster.did]
        assert anti["graph_depth"] == 1
        # The poster is also an ancestor participant, so it cannot claim the
        # child even if the capability check would otherwise pass.
        assert signed_request(client, poster, "POST", f"/api/v1/tasks/{child.json()['id']}/claim", {}).status_code == 403
