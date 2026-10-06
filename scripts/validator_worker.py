#!/usr/bin/env python3
"""Long-running autonomous AgentForge peer validator.

The executor daemon's counterpart: it watches for proofs awaiting peer review,
re-derives the acceptance criteria itself, and submits a signed decision.

What a validator is allowed to do here
--------------------------------------

Peer validation is privileged and the server says so three times over. Before
a decision is accepted the DID must be on the operator allow-list
(``AGENTFORGE_TRUSTED_VALIDATOR_DIDS``), must declare a ``validation``
capability, and must hold an effective ``validator`` role in the operator
registry (an explicit grant, or the development ``OPEN_OPERATORS``
self-registration fallback). It must also be independent of the task: not the
poster, not the executor, no shared operator or infrastructure group. This
daemon cannot grant itself any of that; running it without an operator grant
simply produces a 403 it reports once and then stops.

How it finds work
-----------------

``GET /api/v1/tasks?status=SUBMITTED`` lists the tasks whose proof is pending,
and ``GET /api/v1/tasks/{id}/submissions`` turns a task into the submission ID
and proof hash a decision signature must cover. That index read exists for
exactly this reason: ``GET /api/v1/events`` only ever returns the caller's own
audit rows, so before it a third-party validator had no way to learn what it
was reviewing.

What it checks
--------------

The daemon re-derives, from the task and the submission alone, the same
acceptance facts the server will independently re-check: the committed result
hash, the required outputs, the required evidence kinds, and the acceptance
result schema. It votes ``VERIFIED`` only when every check it ran passed. The
server then runs its own deterministic validation and refuses a ``VERIFIED``
decision whose evidence does not hold up, so this is a first opinion, never
the only one.

Deterministic tasks are skipped: they settle inside the submission
transaction and a late validator decision is correctly rejected with a 409.

Scope: mock-credit marketplace review on a local or operator-provisioned
instance. Submitting decisions here moves no external value.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:  # jsonschema is a declared dependency of the server package
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - optional at validation time
    Draft202012Validator = None  # type: ignore[assignment]

from agentforge_sdk import (
    AgentForgeClient,
    AgentForgeError,
    AgentIdentity,
    ClockDriftError,
)
from agentforge_sdk.crypto import sha256_json

LOGGER = logging.getLogger("agentforge.validator_worker")

#: Task states whose submission may still be peer-reviewed.
REVIEWABLE_STATUSES: tuple[str, ...] = ("SUBMITTED", "DISPUTED")

#: Submission states the server will accept a decision for.
PENDING_SUBMISSION_STATUSES: frozenset[str] = frozenset({"SUBMITTED", "DISPUTED"})

TRANSIENT = (httpx.HTTPError, OSError)


class NotAuthorized(RuntimeError):
    """The server refused this DID as a validator (403)."""


# ---------------------------------------------------------------------------
# Independent acceptance checks
# ---------------------------------------------------------------------------


def _check(checks: list[dict[str, Any]], kind: str, passed: bool, detail: str = "") -> bool:
    entry: dict[str, Any] = {"kind": kind, "passed": bool(passed)}
    if detail and not passed:
        entry["detail"] = detail
    checks.append(entry)
    return bool(passed)


def evaluate(task: dict[str, Any], submission: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Re-derive the task's acceptance criteria against a submission.

    Returns ``(passed, checks, reason_codes)``. Every check is derived from
    the task view and the submission view only -- no server verdict is
    consulted, so this is an independent opinion rather than a restatement.
    """
    acceptance = task.get("acceptance") or {}
    result = submission.get("result")
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    if not _check(
        checks,
        "RESULT_HASH_MATCH",
        isinstance(result, dict) and submission.get("result_hash") == sha256_json(result),
        "stored result hash must be reproducible from the result body",
    ):
        reasons.append("RESULT_HASH_MISMATCH")
        return False, checks, reasons

    expected = acceptance.get("expected_result_hash")
    if expected is not None:
        if not _check(
            checks,
            "EXPECTED_RESULT_HASH_MATCH",
            sha256_json(result) == expected,
            "result does not match the committed expected_result_hash",
        ):
            reasons.append("EXPECTED_RESULT_HASH_MISMATCH")

    required_outputs = [str(item) for item in acceptance.get("required_outputs") or []]
    missing_outputs = [name for name in required_outputs if name not in result]
    if required_outputs and not _check(
        checks,
        "REQUIRED_OUTPUTS_PRESENT",
        not missing_outputs,
        "missing outputs: " + ", ".join(missing_outputs),
    ):
        reasons.append("REQUIRED_OUTPUTS_MISSING")

    required_evidence = [str(item) for item in acceptance.get("required_evidence") or []]
    supplied = {
        str(item.get("kind"))
        for item in submission.get("evidence") or []
        if isinstance(item, dict)
    }
    missing_evidence = [kind for kind in required_evidence if kind not in supplied]
    if required_evidence and not _check(
        checks,
        "REQUIRED_EVIDENCE_PRESENT",
        not missing_evidence,
        "missing evidence kinds: " + ", ".join(missing_evidence),
    ):
        reasons.append("REQUIRED_EVIDENCE_MISSING")

    schema = next(
        (acceptance[key] for key in ("result_schema", "output_schema", "schema") if key in acceptance),
        None,
    )
    if schema is not None and Draft202012Validator is not None:
        try:
            errors = sorted(Draft202012Validator(schema).iter_errors(result), key=str)
            detail = "; ".join(error.message for error in errors[:3])
        except Exception as exc:  # malformed schema is the task's problem
            errors, detail = [exc], f"acceptance schema could not be evaluated: {exc}"
        if not _check(checks, "RESULT_SCHEMA_VALID", not errors, detail):
            reasons.append("RESULT_SCHEMA_VIOLATION")

    passed = all(item["passed"] for item in checks)
    if passed:
        reasons = ["ACCEPTANCE_CRITERIA_SATISFIED"]
    return passed, checks, reasons


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ValidatorConfig:
    base_url: str
    name: str = "agentforge-validator"
    capabilities: tuple[str, ...] = ("validation",)
    chains: tuple[str, ...] = ("local",)
    poll_interval: float = 5.0
    max_poll_interval: float = 60.0
    jitter_ratio: float = 0.25
    timeout: float = 30.0
    task_limit: int = 25
    max_decisions: int | None = None
    reject: bool = False
    once: bool = False

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capabilities": list(self.capabilities),
            "chains": list(self.chains),
        }


@dataclass
class ValidatorStats:
    polls: int = 0
    reviewed: int = 0
    skipped: int = 0
    verified: int = 0
    rejected: int = 0
    withheld: int = 0
    conflicts: int = 0
    clock_drift_retries: int = 0
    poll_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class ValidatorWorker:
    """Poll for pending proofs and submit signed peer-validation decisions."""

    def __init__(
        self,
        identity: AgentIdentity,
        config: ValidatorConfig,
        *,
        client_factory: Any = None,
    ) -> None:
        self.identity = identity
        self.config = config
        self._client_factory = client_factory
        self._stop = threading.Event()
        self.stats = ValidatorStats()

    def new_client(self) -> AgentForgeClient:
        if self._client_factory is not None:
            return self._client_factory()
        return AgentForgeClient(
            self.config.base_url, self.identity, timeout=self.config.timeout
        )

    def request_stop(self) -> None:
        self._stop.set()

    def _retry_on_drift(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except ClockDriftError:
            self.stats.clock_drift_retries += 1
            LOGGER.info("re-signing after server clock-drift rejection")
            return operation(*args, **kwargs)

    # -- discovery --------------------------------------------------------

    def discover(self, client: AgentForgeClient) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for status in REVIEWABLE_STATUSES:
            page = client.list_tasks(status=status, limit=self.config.task_limit)
            for task in page.get("tasks", []):
                found.setdefault(str(task["id"]), task)
        self.stats.polls += 1
        return [task for task in found.values() if self.reviewable(task)]

    def reviewable(self, task: dict[str, Any]) -> bool:
        if task.get("status") not in REVIEWABLE_STATUSES:
            return False
        if task.get("verification_strategy") == "deterministic":
            # Settled atomically at submission time; a decision would 409.
            return False
        if task.get("poster_did") == self.identity.did:
            # Independence: the server rejects a validator who posted the task.
            return False
        return True

    def pending_submission(
        self, client: AgentForgeClient, task_id: str
    ) -> dict[str, Any] | None:
        """Resolve the one submission on this task that is awaiting review."""
        index = client.list_task_submissions(task_id)
        for row in index.get("submissions", []):
            if row.get("status") in PENDING_SUBMISSION_STATUSES:
                return row
        return None

    # -- review -----------------------------------------------------------

    def review(self, client: AgentForgeClient, task: dict[str, Any]) -> str | None:
        """Review one task. Returns the decision submitted, or ``None``."""
        task_id = str(task["id"])
        row = self.pending_submission(client, task_id)
        if row is None:
            self.stats.skipped += 1
            return None
        if row.get("executor_did") == self.identity.did:
            # Independence again: never review your own proof.
            self.stats.skipped += 1
            return None

        submission = client.get_submission(str(row["submission_id"]))
        passed, checks, reasons = evaluate(task, submission)
        self.stats.reviewed += 1

        if not passed and not self.config.reject:
            # Default posture: an opinion that something failed is not the
            # same as the authority to slash someone's escrow. Withhold and
            # let a human or an explicitly configured validator decide.
            self.stats.withheld += 1
            LOGGER.warning(
                "withholding decision for task=%s submission=%s failed=%s "
                "(pass --reject to vote REJECTED automatically)",
                task_id,
                row["submission_id"],
                ",".join(reasons) or "unknown",
            )
            return None

        decision = "VERIFIED" if passed else "REJECTED"
        try:
            self._retry_on_drift(
                client.validate_task,
                task_id,
                decision=decision,
                submission_id=str(row["submission_id"]),
                evidence_hash=str(row["proof_hash"]),
                checks=checks,
                reason_codes=reasons,
            )
        except AgentForgeError as exc:
            status = getattr(exc, "status_code", None)
            if status == 403:
                raise NotAuthorized(str(exc)) from exc
            if status in {404, 409, 422}:
                # Another validator won the race, the submission already
                # settled, or the server's own deterministic check disagreed
                # with a VERIFIED vote. All are ordinary outcomes.
                self.stats.conflicts += 1
                LOGGER.info("decision on %s not applied: %s", task_id, exc)
                return None
            raise
        if decision == "VERIFIED":
            self.stats.verified += 1
        else:
            self.stats.rejected += 1
        LOGGER.info(
            "decided task=%s submission=%s decision=%s reasons=%s",
            task_id,
            row["submission_id"],
            decision,
            ",".join(reasons),
        )
        return decision

    def _reached_limit(self) -> bool:
        limit = self.config.max_decisions
        if limit is None:
            return False
        return (self.stats.verified + self.stats.rejected) >= limit

    def _sleep_for(self, delay: float) -> float:
        spread = delay * self.config.jitter_ratio
        return max(0.0, delay + random.uniform(-spread, spread))

    def run(self) -> ValidatorStats:
        client = self.new_client()
        delay = self.config.poll_interval
        try:
            while not self._stop.is_set():
                try:
                    for task in self.discover(client):
                        if self._stop.is_set() or self._reached_limit():
                            break
                        self.review(client, task)
                    delay = self.config.poll_interval
                except NotAuthorized as exc:
                    LOGGER.error(
                        "this DID is not an approved validator on %s: %s. "
                        "An operator must add it to AGENTFORGE_TRUSTED_VALIDATOR_DIDS "
                        "and grant the validator registry role.",
                        self.config.base_url,
                        exc,
                    )
                    break
                except AgentForgeError as exc:
                    self.stats.poll_errors += 1
                    delay = min(self.config.max_poll_interval, max(delay * 2, self.config.poll_interval))
                    LOGGER.error("poll failed (%s); next poll in %.1fs", exc, delay)
                except TRANSIENT as exc:
                    self.stats.poll_errors += 1
                    delay = min(self.config.max_poll_interval, max(delay * 2, self.config.poll_interval))
                    LOGGER.error("poll transport error (%s); next poll in %.1fs", exc, delay)
                if self.config.once or self._reached_limit():
                    break
                self._stop.wait(self._sleep_for(delay))
        finally:
            self._stop.set()
            client.close()
        return self.stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_or_create_identity(path: Path) -> tuple[AgentIdentity, bool]:
    if path.exists():
        return AgentIdentity.load(path), False
    identity = AgentIdentity.generate()
    identity.save(path)
    return identity, True


def ensure_registered(client: AgentForgeClient, config: ValidatorConfig, mode: str) -> None:
    if mode == "never":
        return
    if mode == "auto":
        try:
            client.get_agent()
            LOGGER.info("identity already registered")
            return
        except AgentForgeError as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
    profile = client.register(config.manifest())
    LOGGER.info("registered did=%s name=%s", profile.get("did"), profile.get("name"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--identity-path", type=Path, required=True)
    parser.add_argument("--name", default="agentforge-validator")
    parser.add_argument("--capabilities", default="validation")
    parser.add_argument("--chains", default="local")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-poll-interval", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--task-limit", type=int, default=25)
    parser.add_argument("--max-decisions", type=int, default=None)
    parser.add_argument(
        "--reject",
        action="store_true",
        help="also vote REJECTED on failing proofs instead of withholding",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--register", choices=("auto", "always", "never"), default="auto")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = ValidatorConfig(
        base_url=args.base_url,
        name=args.name,
        capabilities=tuple(item.strip() for item in args.capabilities.split(",") if item.strip()),
        chains=tuple(item.strip() for item in args.chains.split(",") if item.strip()),
        poll_interval=args.poll_interval,
        max_poll_interval=args.max_poll_interval,
        timeout=args.timeout,
        task_limit=args.task_limit,
        max_decisions=args.max_decisions,
        reject=args.reject,
        once=args.once,
    )

    try:
        identity, created = load_or_create_identity(args.identity_path)
    except (AgentForgeError, OSError, ValueError) as exc:
        print(f"validator startup failed: {exc}", file=sys.stderr)
        return 1
    LOGGER.info("identity %s did=%s", "created" if created else "loaded", identity.did)

    worker = ValidatorWorker(identity, config)

    def handle_signal(signum: int, _frame: object) -> None:
        LOGGER.info("%s received; stopping after the current review", signal.Signals(signum).name)
        worker.request_stop()

    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(received, handle_signal)
        except ValueError:
            pass

    registrar = worker.new_client()
    try:
        ensure_registered(registrar, config, args.register)
    except (AgentForgeError, *TRANSIENT) as exc:
        print(f"registration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        registrar.close()

    LOGGER.info("reviewing %s every %.1fs", config.base_url, config.poll_interval)
    stats = worker.run()
    print(json.dumps({"did": identity.did, "stats": stats.as_dict()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
