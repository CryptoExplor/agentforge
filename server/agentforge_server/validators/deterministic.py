"""Deterministic, provider-independent submission checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from ..crypto import sha256_json
from ..models import InferenceSession, Submission, Task


@dataclass
class DeterministicValidation:
    checks: list[dict[str, Any]]
    fatal: bool


def _check(checks: list[dict[str, Any]], code: str, passed: bool, detail: str = "") -> None:
    item: dict[str, Any] = {"code": code, "result": "PASS" if passed else "FAIL"}
    if detail:
        item["detail"] = detail
    checks.append(item)


def _nonnegative_decimal(value: Any) -> bool:
    try:
        number = Decimal(str(value))
        return number.is_finite() and number >= 0
    except (InvalidOperation, ValueError):
        return False


def _path_value(document: Any, path: str) -> tuple[bool, Any]:
    """Read a dotted object path without evaluating user code."""
    current = document
    for component in path.split("."):
        if not isinstance(current, dict) or component not in current:
            return False, None
        current = current[component]
    return True, current


def _validate_result_schema(result: dict[str, Any], schema: Any) -> tuple[bool, str]:
    from .result_schema import validate_result_schema
    return validate_result_schema(result, schema)


def _validate_acceptance(
    checks: list[dict[str, Any]],
    task: Task,
    submission: Submission,
) -> None:
    acceptance = task.acceptance or {}
    result = submission.result or {}

    required_outputs = acceptance.get("required_outputs", acceptance.get("required_output_fields", []))
    if not isinstance(required_outputs, list):
        _check(checks, "REQUIRED_OUTPUTS_PRESENT", False, "required_outputs must be a list")
    else:
        missing_outputs = [key for key in required_outputs if not isinstance(key, str) or not _path_value(result, key)[0]]
        _check(
            checks,
            "REQUIRED_OUTPUTS_PRESENT",
            not missing_outputs,
            "missing: " + ", ".join(map(str, missing_outputs)) if missing_outputs else "",
        )

    expected_outputs = acceptance.get("expected_outputs")
    if expected_outputs is not None:
        passed = isinstance(expected_outputs, dict) and all(
            _path_value(result, str(key))[0] and _path_value(result, str(key))[1] == value
            for key, value in (expected_outputs.items() if isinstance(expected_outputs, dict) else [])
        )
        _check(checks, "EXPECTED_OUTPUTS_MATCH", passed, "declared expected output values do not match")

    for schema_key in ("result_schema", "output_schema", "schema"):
        if schema_key in acceptance:
            passed, detail = _validate_result_schema(result, acceptance[schema_key])
            _check(checks, "RESULT_SCHEMA_VALID", passed, detail)

    required_evidence = acceptance.get("required_evidence", [])
    if not isinstance(required_evidence, list):
        _check(checks, "REQUIRED_EVIDENCE_PRESENT", False, "required_evidence must be a list")
        return

    evidence = submission.evidence or []
    evidence_by_kind = {
        str(item.get("kind")): item
        for item in evidence
        if isinstance(item, dict) and item.get("kind")
    }
    missing_evidence: list[str] = []
    invalid_evidence: list[str] = []
    for requirement in required_evidence:
        if isinstance(requirement, str):
            kind = requirement
            requirement_data: dict[str, Any] = {}
        elif isinstance(requirement, dict) and requirement.get("kind"):
            kind = str(requirement["kind"])
            requirement_data = requirement
        else:
            missing_evidence.append(str(requirement))
            continue
        item = evidence_by_kind.get(kind)
        if item is None:
            missing_evidence.append(kind)
            continue
        for field in requirement_data.get("required_fields", []):
            if field not in item:
                invalid_evidence.append(f"{kind}.{field}")
        expected_hash = requirement_data.get("content_hash")
        if expected_hash is not None and item.get("content_hash") != expected_hash:
            invalid_evidence.append(f"{kind}.content_hash")

    _check(
        checks,
        "REQUIRED_EVIDENCE_PRESENT",
        not missing_evidence,
        "missing: " + ", ".join(missing_evidence) if missing_evidence else "",
    )
    _check(
        checks,
        "EVIDENCE_STRUCTURE_VALID",
        not invalid_evidence,
        "invalid: " + ", ".join(invalid_evidence) if invalid_evidence else "",
    )


def validate_submission(db: Session, task: Task, submission: Submission) -> DeterministicValidation:
    checks: list[dict[str, Any]] = []

    proof = submission.proof or {}
    _check(
        checks,
        "ACCEPTANCE_HASH_MATCH",
        task.acceptance_hash == sha256_json(task.acceptance),
        "stored acceptance hash must be reproducible",
    )
    _check(
        checks,
        "PROOF_IDENTITY_MATCH",
        proof.get("task_id") == task.id
        and proof.get("submission_id") == submission.id
        and proof.get("executor_did") == submission.executor_did
        and proof.get("evidence") == submission.evidence
        and proof.get("inference_session_ids", []) == submission.inference_session_ids
        and proof.get("created_at") == submission.created_at,
        "proof identity and signed fields must match the stored submission",
    )
    _check(
        checks,
        "INPUT_HASH_MATCH",
        proof.get("input_hash") == sha256_json(task.input_data),
        "proof input hash must match the task input",
    )
    _check(
        checks,
        "RESULT_HASH_MATCH",
        proof.get("result_hash") == submission.result_hash == sha256_json(submission.result),
        "proof/result hashes must agree",
    )
    _check(
        checks,
        "PROOF_HASH_MATCH",
        sha256_json(proof) == submission.proof_hash,
        "stored proof hash must be reproducible",
    )

    _validate_acceptance(checks, task, submission)

    sessions_ok = True
    session_failures: list[str] = []
    for session_id in submission.inference_session_ids or []:
        session = db.get(InferenceSession, session_id)
        if not session or session.task_id != task.id or session.executor_did != submission.executor_did:
            sessions_ok = False
            session_failures.append(str(session_id))
            continue
        if session.submission_id != submission.id:
            sessions_ok = False
            session_failures.append(f"{session_id}:submission")
        if session.status != "COMPLETED" or not session.result or not session.receipt:
            sessions_ok = False
            session_failures.append(f"{session_id}:incomplete")
            continue
        receipt = session.receipt
        if receipt.get("request_id") != session.id or receipt.get("provider") != session.provider:
            sessions_ok = False
            session_failures.append(f"{session_id}:receipt_identity")
        if receipt.get("model_ref") != (session.request or {}).get("model_ref"):
            sessions_ok = False
            session_failures.append(f"{session_id}:model")
        if receipt.get("result_hash") != sha256_json(session.result):
            sessions_ok = False
            session_failures.append(f"{session_id}:result_hash")
        for field in ("requested_compute", "measured_compute", "paid_compute", "verified_compute"):
            if not _nonnegative_decimal(receipt.get(field, "-1")):
                sessions_ok = False
                session_failures.append(f"{session_id}:{field}")

    _check(
        checks,
        "INFERENCE_SESSIONS_VALID",
        sessions_ok,
        "invalid: " + ", ".join(session_failures) if session_failures else "",
    )

    deadline_ok = not task.deadline or submission.created_at <= task.deadline
    _check(checks, "SUBMISSION_BEFORE_DEADLINE", deadline_ok, "submission arrived after the task deadline")

    fatal = any(item["result"] == "FAIL" for item in checks)
    return DeterministicValidation(checks=checks, fatal=fatal)
