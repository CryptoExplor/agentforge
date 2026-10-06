#!/usr/bin/env python3
"""Run one complete AgentForge marketplace exchange through the public SDK.

The default deterministic strategy is suitable for a local development server
and for a protected staging instance whose identities and mock balances were
pre-provisioned by an operator. Peer review additionally requires the validator
DID allow-list and an operator-attributed ACTIVE validator grant.

This script never enables registration, creates credits, changes policy, or
prints private keys. It is a demo/probe, not a staging bootstrap tool.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from agentforge_sdk import AgentForgeClient, AgentForgeError, AgentIdentity
from agentforge_sdk.crypto import sha256_json


ROLE_MANIFESTS = {
    "poster": {"name": "simulation-poster", "capabilities": ["research"], "chains": ["local"]},
    "executor": {
        "name": "simulation-executor",
        "capabilities": ["marketplace_demo"],
        "chains": ["local"],
    },
    "validator": {
        "name": "simulation-validator",
        "capabilities": ["validation"],
        "chains": ["local"],
    },
}


def load_or_create_identities(directory: Path) -> dict[str, AgentIdentity]:
    """Load stable demo identities or create them atomically with mode 0600."""
    directory.mkdir(parents=True, exist_ok=True)
    identities: dict[str, AgentIdentity] = {}
    for role in ROLE_MANIFESTS:
        path = directory / f"{role}.json"
        if path.exists():
            identity = AgentIdentity.load(path)
        else:
            identity = AgentIdentity.generate()
            identity.save(path)
        identities[role] = identity
    return identities


def task_payload(result: dict[str, Any], strategy: str) -> dict[str, Any]:
    return {
        "kind": "deterministic",
        "visibility": "public",
        "origin": "external",
        "verification_strategy": strategy,
        "required_capabilities": ["marketplace_demo"],
        "chains": ["local"],
        "input": {"question": "Return the AgentForge readiness marker."},
        "acceptance": {
            "required_outputs": ["answer"],
            "required_evidence": ["demo_receipt"],
            "expected_result_hash": sha256_json(result),
            "result_schema": {
                "type": "object",
                "properties": {"answer": {"const": "ready"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
        "demand_provenance": {
            "type": "synthetic_demo",
            "level": 1,
            "source": "scripts/simulate_marketplace.py",
            "source_ref": f"simulation-{int(time.time())}",
        },
        "generation_policy": {
            "economic_eligibility": "DEMO_ONLY",
            "minimum_provenance_level": 0,
            "synthetic_demo": True,
        },
        "economics": {
            "mode": "BOUNTY",
            "reward": {"amount": "10", "asset": "MOCK"},
            "service_fee_mode": "bps",
            "service_fee_bps": 500,
            "security_deposit": {"amount": "1", "asset": "MOCK"},
            "inference_budget": {"amount": "2", "asset": "MOCK"},
        },
        "deadline": time.time() + 600,
    }


def as_decimal(response: dict[str, Any]) -> Decimal:
    return Decimal(response["balance"])


def run_simulation(
    base_url: str,
    identity_dir: Path,
    *,
    strategy: str = "deterministic",
    timeout: float = 30.0,
) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    health_response = httpx.get(f"{base_url}/health", timeout=timeout)
    health_response.raise_for_status()
    health = health_response.json()
    health_clock = health.get("clock") or {}
    measured_skew = health_clock.get("database_skew_seconds")
    skew_tolerance = health_clock.get("database_skew_tolerance_seconds")
    unhealthy_clock = (
        health_clock.get("database_error")
        or measured_skew is None
        or skew_tolerance is None
        or abs(float(measured_skew)) > float(skew_tolerance)
    )
    if health.get("status") != "ok" or unhealthy_clock:
        raise RuntimeError("server health/database clock check did not pass")

    identities = load_or_create_identities(identity_dir)
    clients = {
        role: AgentForgeClient(base_url, identity, timeout=timeout)
        for role, identity in identities.items()
    }
    try:
        for role, client in clients.items():
            client.register(ROLE_MANIFESTS[role])

        starting = {role: as_decimal(client.balance()) for role, client in clients.items()}
        if starting["poster"] < Decimal("13"):
            raise RuntimeError(
                "poster needs at least 13 MOCK credits; protected staging has no faucet, "
                "so an operator must provision this pre-enrolled identity"
            )

        result = {"answer": "ready"}
        task = clients["poster"].create_task(task_payload(result, strategy))
        if task["status"] != "FUNDED" or task["escrow"]["reserved_total"] != "13":
            raise RuntimeError("task did not enter the expected funded escrow state")

        claim = clients["executor"].claim(task["id"])
        clients["executor"].heartbeat(claim["claim_id"])
        inference = clients["executor"].infer(
            task["id"], {"provider": "mock", "requested_compute": "1"}
        )
        submission = clients["executor"].submit(
            task["id"],
            result=result,
            evidence=[{"kind": "demo_receipt", "content_hash": "sha256:agentforge-ready"}],
            inference_session_ids=[inference["session_id"]],
        )

        if strategy == "peer_review":
            clients["validator"].validate_task(
                task["id"],
                decision="VERIFIED",
                submission_id=submission["submission_id"],
                checks=[{"kind": "demo_acceptance", "passed": True}],
                reason_codes=["ACCEPTANCE_CRITERIA_SATISFIED"],
            )
        elif submission.get("verification", {}).get("decision") != "VERIFIED":
            raise RuntimeError(f"deterministic verification failed: {submission.get('verification')}")

        final_task = clients["poster"].get_task(task["id"])
        escrow = final_task["escrow"]
        ending = {role: as_decimal(client.balance()) for role, client in clients.items()}
        executor_reputation = clients["poster"].reputation(identities["executor"].did)

        expected_escrow = {
            "status": "RELEASED",
            "reserved_total": "13",
            "released_amount": "9.5",
            "platform_fee_amount": "0.5",
            "refunded_amount": "3",
        }
        for field, expected in expected_escrow.items():
            if escrow.get(field) != expected:
                raise RuntimeError(f"unexpected escrow {field}: {escrow.get(field)!r}, expected {expected!r}")
        if final_task["status"] != "VERIFIED":
            raise RuntimeError(f"unexpected final task status: {final_task['status']}")
        if ending["poster"] != starting["poster"] - Decimal("10"):
            raise RuntimeError("poster balance does not match reserve/refund accounting")
        if ending["executor"] != starting["executor"] + Decimal("9.5"):
            raise RuntimeError("executor balance does not match payout net of fee")
        if ending["validator"] != starting["validator"]:
            raise RuntimeError("validator balance changed unexpectedly")
        if executor_reputation.get("overall") != 1.0:
            raise RuntimeError("executor reputation was not updated")

        return {
            "ok": True,
            "base_url": base_url,
            "strategy": strategy,
            "task_id": task["id"],
            "claim_id": claim["claim_id"],
            "submission_id": submission["submission_id"],
            "identities": {role: identity.did for role, identity in identities.items()},
            "escrow": escrow,
            "balances": {
                role: {"before": str(starting[role]), "after": str(ending[role])}
                for role in clients
            },
            "executor_reputation": executor_reputation,
            "health_clock": health.get("clock"),
        }
    finally:
        for client in clients.values():
            client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--identity-dir",
        type=Path,
        help="load/create poster, executor and validator identities here; omit for ephemeral keys",
    )
    parser.add_argument(
        "--strategy",
        choices=("deterministic", "peer_review"),
        default="deterministic",
        help="peer_review requires explicit staging validator authorization",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.identity_dir:
            result = run_simulation(
                args.base_url, args.identity_dir, strategy=args.strategy, timeout=args.timeout
            )
        else:
            with tempfile.TemporaryDirectory(prefix="agentforge-simulation-") as temporary:
                result = run_simulation(
                    args.base_url, Path(temporary), strategy=args.strategy, timeout=args.timeout
                )
    except (AgentForgeError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(f"simulation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
