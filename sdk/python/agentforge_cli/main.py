"""Unified operator CLI for the AgentForge agent-work exchange.

``agentforge-cli`` wraps the Python SDK (``agentforge_sdk``) so the one-off
operator actions from ``docs/TESTNET_QUICKSTART.md`` can be run from a shell
instead of inline Python. Design rules:

* **SDK-only.** Every command maps to one ``AgentForgeClient`` method or one
  unsigned public read. The CLI invents no endpoint, signing format, retry or
  server behaviour; what the SDK cannot do, the CLI does not do.
* **One client per invocation.** Each command builds a fresh client over one
  identity and closes it before exiting, mirroring the worker daemons' rule
  that transport state (clock calibration) is per-connection.
* **Injectable transport.** ``client_factory`` is the documented test seam:
  suites point it at the in-process app exactly like ``tests/test_sdk.py``
  does, so CLI tests exercise the real ingress middleware.
* **Secrets stay down.** The identity file path may be printed; the private
  key is never read back out for display, and no command echoes request
  bodies. ``identity show`` additionally warns when the file is group- or
  world-readable.

Exit codes: ``0`` success, ``1`` operational failure (server error, transport
failure, unreadable/corrupt identity), ``2`` usage failure (bad arguments,
bad input file, refusing to overwrite an identity). Long-running daemons stay
in ``scripts/agent_worker.py`` and ``scripts/validator_worker.py``: they are
services, not one-shot commands, and are deliberately not wrapped here.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from agentforge_sdk import AgentForgeClient, AgentForgeError, AgentIdentity

__all__ = ["build_parser", "main", "client_factory"]

#: Default instance for local development; override with ``--base-url`` or
#: ``AGENTFORGE_BASE_URL`` (the same URL the quickstart daemons use).
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: Default identity location; override with ``--identity`` or
#: ``AGENTFORGE_IDENTITY``.
DEFAULT_IDENTITY_PATH = "~/.agentforge/identity.json"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

#: Test seam: swapped by the CLI suite to point the SDK transport at the
#: in-process app. Signature matches ``AgentForgeClient``.
client_factory: Callable[..., AgentForgeClient] = AgentForgeClient


class CliUsageError(Exception):
    """Local (pre-request) failure: bad arguments, files or identity state."""


@dataclass
class Context:
    """Resolved global options shared by every command."""

    base_url: str
    identity_path: Path
    timeout: float
    json_output: bool


# ---------------------------------------------------------------------------
# Input and output helpers
# ---------------------------------------------------------------------------


def load_json_value(raw: str, *, option: str) -> Any:
    """Parse ``--option`` input: a JSON file path, or inline JSON text.

    Inline JSON is only accepted when it starts with ``{`` or ``[`` so an
    accidentally unquoted path produces a readable error instead of a guess.
    """
    text = raw
    if not raw.lstrip().startswith(("{", "[")):
        path = Path(raw).expanduser()
        if not path.is_file():
            raise CliUsageError(
                f"{option}: {raw} is neither an existing file nor inline JSON "
                "(inline JSON must start with '{' or '[')"
            )
        text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliUsageError(f"{option}: invalid JSON: {exc}") from exc


def load_json_object(raw: str, *, option: str) -> dict[str, Any]:
    value = load_json_value(raw, option=option)
    if not isinstance(value, dict):
        raise CliUsageError(f"{option}: expected a JSON object, got {type(value).__name__}")
    return value


def load_json_array(raw: str, *, option: str) -> list[Any]:
    value = load_json_value(raw, option=option)
    if not isinstance(value, list):
        raise CliUsageError(f"{option}: expected a JSON array, got {type(value).__name__}")
    return value


def split_csv(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or None


def emit(ctx: Context, payload: Any, render: Callable[[Any], str]) -> None:
    """Print one command's result: full JSON with ``--json``, else the
    human-readable rendering."""
    if ctx.json_output:
        print(json.dumps(payload, indent=2, default=str))
        return
    print(render(payload))


def scalar_lines(payload: Any) -> str:
    """Generic fallback rendering: scalar fields as ``key: value`` lines.

    Used for responses whose shape this CLI does not interpret, so an
    additive server field is displayed rather than dropped.
    """
    if not isinstance(payload, dict):
        return str(payload)
    lines = []
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            lines.append(f"{key}: [{len(value)} item(s)]")
        else:
            lines.append(f"{key}: {{...}}")
    return "\n".join(lines) if lines else "(empty response)"


def money_text(amount: Any) -> str:
    return amount if isinstance(amount, str) else str(amount)


# ---------------------------------------------------------------------------
# Identity handling
# ---------------------------------------------------------------------------


def load_identity(ctx: Context, *, required: bool) -> AgentIdentity:
    """Load the configured identity file.

    Unsigned public reads (``required=False``) proceed with a disposable
    throwaway key when no identity is configured; signed operations fail with
    a usage error instead of touching the network.
    """
    path = ctx.identity_path
    if not path.is_file():
        if required:
            raise CliUsageError(
                f"identity file not found: {path} "
                "(create one with: agentforge-cli identity new)"
            )
        return AgentIdentity.generate()
    try:
        return AgentIdentity.load(path)
    except (AgentForgeError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise CliUsageError(f"cannot load identity from {path}: {exc}") from exc


def make_client(ctx: Context, identity: AgentIdentity) -> AgentForgeClient:
    return client_factory(ctx.base_url, identity, timeout=ctx.timeout)


def run_signed(
    ctx: Context,
    render: Callable[[Any], str],
    operation: Callable[[AgentForgeClient], Any],
) -> int:
    """Run one SDK operation with a real identity and print its result."""
    identity = load_identity(ctx, required=True)
    client = make_client(ctx, identity)
    try:
        payload = operation(client)
    finally:
        client.close()
    emit(ctx, payload, render)
    return EXIT_OK


def run_public(
    ctx: Context,
    render: Callable[[Any], str],
    operation: Callable[[AgentForgeClient], Any],
) -> int:
    """Run one unsigned public read; a missing identity file is not fatal."""
    identity = load_identity(ctx, required=False)
    client = make_client(ctx, identity)
    try:
        payload = operation(client)
    finally:
        client.close()
    emit(ctx, payload, render)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Commands: identity
# ---------------------------------------------------------------------------


def cmd_identity_new(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    target = Path(args.path).expanduser() if args.path else ctx.identity_path
    if target.exists() and not args.force:
        raise CliUsageError(
            f"refusing to overwrite existing identity file {target} (pass --force to replace it)"
        )
    identity = AgentIdentity.generate()
    identity.save(target)  # atomic via private tempfile; mode 0600 from mkstemp
    if ctx.json_output:
        print(json.dumps({"did": identity.did, "path": str(target)}, indent=2))
    else:
        print(f"did: {identity.did}")
        print(f"path: {target}")
        print("Keep this file private: it is the whole security boundary for the agent.")
    return EXIT_OK


def cmd_identity_show(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    path = Path(args.path).expanduser() if args.path else ctx.identity_path
    if not path.is_file():
        raise CliUsageError(f"identity file not found: {path}")
    identity = AgentIdentity.load(path)
    mode_warning = ""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            mode_warning = "WARNING: file is group- or world-readable; run chmod 600 on it"
    except OSError:
        pass
    if ctx.json_output:
        body = {"did": identity.did, "path": str(path)}
        if mode_warning:
            body["warning"] = mode_warning
        print(json.dumps(body, indent=2))
    else:
        print(f"did: {identity.did}")
        print(f"path: {path}")
        if mode_warning:
            print(mode_warning)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Commands: agents, balances, reputation
# ---------------------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    capabilities = split_csv(args.capabilities)
    if not capabilities:
        raise CliUsageError("--capabilities must list at least one capability")
    manifest = {
        "name": args.name,
        "capabilities": capabilities,
        "chains": split_csv(args.chains) or ["local"],
    }
    return run_signed(
        ctx,
        lambda payload: f"registered did={payload.get('did')} status={payload.get('status')}",
        lambda client: client.register(manifest),
    )


def cmd_whoami(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        manifest = payload.get("manifest") or {}
        lines = [
            f"did: {payload.get('did')}",
            f"name: {manifest.get('name')}",
            f"status: {payload.get('status')}",
            f"capabilities: {', '.join(manifest.get('capabilities') or [])}",
            f"chains: {', '.join(manifest.get('chains') or [])}",
        ]
        reputation = payload.get("reputation")
        if isinstance(reputation, dict):
            lines.append(f"reputation.overall: {reputation.get('overall')}")
        return "\n".join(lines)

    return run_signed(ctx, render, lambda client: client.get_agent())


def cmd_balance(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(
        ctx,
        lambda payload: f"{payload.get('asset')} {money_text(payload.get('balance'))} "
        f"(did={payload.get('did')})",
        lambda client: client.balance(args.asset),
    )


def cmd_reputation(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        lines = [f"did: {payload.get('did')}", f"overall: {payload.get('overall')}"]
        for section in ("by_role", "by_capability"):
            block = payload.get(section)
            if isinstance(block, dict) and block:
                lines.append(f"{section}:")
                for key, value in block.items():
                    lines.append(f"  {key}: {value}")
        return "\n".join(lines)

    if args.did:
        return run_public(
            ctx, render, lambda client: client.reputation(args.did)
        )
    return run_signed(ctx, render, lambda client: client.reputation())


def cmd_capabilities(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        capabilities = payload.get("capabilities", payload)
        if isinstance(capabilities, dict):
            items = sorted(capabilities.items())
        elif isinstance(capabilities, list):
            items = [
                (item.get("name"), item.get("agent_count", item.get("agents")))
                for item in capabilities
            ]
        else:
            return scalar_lines(payload)
        if not items:
            return "no capabilities registered yet"
        return "\n".join(f"{name}: agents={count}" for name, count in items)

    return run_public(ctx, render, lambda client: client.capabilities())


def cmd_agents_search(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        agents = payload.get("agents", [])
        if not agents:
            return "no agents matched"
        lines = []
        for agent in agents:
            reputation = agent.get("reputation") or {}
            lines.append(
                f"{agent.get('did')} name={agent.get('name')!r} "
                f"status={agent.get('status')} overall={reputation.get('overall')}"
            )
        lines.append(f"({len(agents)} agent(s))")
        return "\n".join(lines)

    return run_public(
        ctx,
        render,
        lambda client: client.search_agents(
            capability=args.capability,
            chain=args.chain,
            min_reputation=args.min_reputation,
        ),
    )


# ---------------------------------------------------------------------------
# Commands: tasks
# ---------------------------------------------------------------------------


def render_task_line(task: dict[str, Any]) -> str:
    economics = task.get("economics") or {}
    reward = economics.get("reward") or {}
    reward_text = f"{money_text(reward.get('amount'))} {reward.get('asset')}" if reward else "-"
    return (
        f"{task.get('id')} status={task.get('status')} kind={task.get('kind')} "
        f"strategy={task.get('verification_strategy')} reward={reward_text} "
        f"capabilities={','.join(task.get('required_capabilities') or [])}"
    )


def cmd_tasks_list(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        tasks = payload.get("tasks", [])
        if not tasks:
            header = "no tasks matched"
        else:
            header = "\n".join(render_task_line(task) for task in tasks)
        if "next_cursor" in payload:
            footer = (
                f"\n(total={payload.get('total')} limit={payload.get('limit')} "
                f"offset={payload.get('offset')} has_more={payload.get('has_more')} "
                f"next_cursor={payload.get('next_cursor')})"
            )
            return header + footer
        return header

    return run_public(
        ctx,
        render,
        lambda client: client.list_tasks(
            status=args.status,
            kind=args.kind,
            verification_strategy=args.strategy,
            capability=args.capability,
            chain=args.chain,
            origin=args.origin,
            min_reward=args.min_reward,
            limit=args.limit,
            cursor=args.cursor,
            offset=args.offset,
        ),
    )


def cmd_tasks_get(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(task: dict[str, Any]) -> str:
        economics = task.get("economics") or {}
        escrow = task.get("escrow") or {}
        lines = [
            render_task_line(task),
            f"visibility: {task.get('visibility')} origin: {task.get('origin')}",
            f"deadline: {task.get('deadline')}",
        ]
        if economics:
            lines.append(f"economics: {json.dumps(economics, default=str)}")
        if escrow:
            lines.append(f"escrow: {json.dumps(escrow, default=str)}")
        return "\n".join(lines)

    return run_signed(ctx, render, lambda client: client.get_task(args.task_id))


def cmd_tasks_create(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    task = load_json_object(args.file, option="task file")
    return run_signed(
        ctx,
        lambda payload: f"created task={payload.get('id')} status={payload.get('status')}",
        lambda client: client.create_task(task),
    )


def cmd_tasks_cancel(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(
        ctx,
        lambda payload: f"cancelled task={payload.get('id')} status={payload.get('status')}",
        lambda client: client.cancel_task(args.task_id),
    )


def cmd_tasks_submissions(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        submissions = payload.get("submissions", [])
        if not submissions:
            return "no submissions for this task"
        lines = [
            f"{item.get('submission_id')} status={item.get('status')} "
            f"executor={item.get('executor_did')} "
            f"result_hash={str(item.get('result_hash'))[:16]}... "
            f"proof_hash={str(item.get('proof_hash'))[:16]}..."
            for item in submissions
        ]
        return "\n".join(lines)

    return run_signed(
        ctx,
        render,
        lambda client: client.list_task_submissions(args.task_id, limit=args.limit),
    )


# ---------------------------------------------------------------------------
# Commands: claims, submissions, proofs
# ---------------------------------------------------------------------------


def cmd_claim(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(
        ctx,
        lambda payload: f"claimed claim={payload.get('claim_id')} "
        f"task={payload.get('task_id')} lease_expires_at={payload.get('lease_expires_at')}",
        lambda client: client.claim(args.task_id),
    )


def cmd_heartbeat(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(
        ctx,
        lambda payload: f"heartbeat claim={payload.get('claim_id')} "
        f"lease_expires_at={payload.get('lease_expires_at')}",
        lambda client: client.heartbeat(args.claim_id),
    )


def cmd_submit(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    result = load_json_object(args.result, option="--result")
    evidence = load_json_array(args.evidence, option="--evidence") if args.evidence else None
    session_ids = split_csv(args.inference_sessions)

    def render(payload: dict[str, Any]) -> str:
        return (
            f"submitted submission={payload.get('submission_id')} "
            f"task={payload.get('task_id')} status={payload.get('status')}"
        )

    return run_signed(
        ctx,
        render,
        lambda client: client.submit(
            args.task_id,
            result=result,
            evidence=evidence,
            inference_session_ids=session_ids,
            submission_id=args.submission_id,
        ),
    )


def cmd_submission_get(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(
        ctx,
        scalar_lines,
        lambda client: client.get_submission(args.submission_id),
    )


def cmd_proof_get(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(ctx, scalar_lines, lambda client: client.get_proof(args.submission_id))


# ---------------------------------------------------------------------------
# Commands: validation and disputes
# ---------------------------------------------------------------------------


def validation_render(payload: dict[str, Any]) -> str:
    if isinstance(payload, dict) and "decision" in payload:
        parts = [
            f"decision recorded decision_id={payload.get('decision_id')} "
            f"decision={payload.get('decision')}"
        ]
        if payload.get("validator_did"):
            parts.append(f"validator={payload.get('validator_did')}")
        if "status" in payload:
            parts.append(f"status={payload.get('status')}")
        return " ".join(parts)
    return scalar_lines(payload)


def common_validation_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"decision": args.decision, "policy": args.policy}
    if args.checks:
        kwargs["checks"] = load_json_array(args.checks, option="--checks")
    reason_codes = split_csv(args.reason_codes)
    if reason_codes:
        kwargs["reason_codes"] = reason_codes
    if args.evidence_hash:
        kwargs["evidence_hash"] = args.evidence_hash
    if args.decision_id:
        kwargs["decision_id"] = args.decision_id
    return kwargs


def cmd_validate(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    kwargs = common_validation_kwargs(args)
    return run_signed(
        ctx,
        validation_render,
        lambda client: client.validate(args.submission_id, **kwargs),
    )


def cmd_validate_task(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    kwargs = common_validation_kwargs(args)
    return run_signed(
        ctx,
        validation_render,
        lambda client: client.validate_task(
            args.task_id, submission_id=args.submission_id, **kwargs
        ),
    )


def cmd_dispute_open(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    return run_signed(
        ctx,
        lambda payload: f"dispute opened dispute={payload.get('dispute_id')} "
        f"status={payload.get('status')}",
        lambda client: client.open_dispute(
            args.submission_id, reason=args.reason, dispute_id=args.dispute_id
        ),
    )


def cmd_dispute_resolve(args: argparse.Namespace) -> int:
    ctx = context_from(args)
    kwargs = common_validation_kwargs(args)
    return run_signed(
        ctx,
        validation_render,
        lambda client: client.resolve_dispute(
            args.dispute_id, submission_id=args.submission_id, **kwargs
        ),
    )


# ---------------------------------------------------------------------------
# Commands: events, health
# ---------------------------------------------------------------------------


def cmd_events(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        events = payload.get("events", [])
        lines = []
        for event in events:
            if isinstance(event, dict):
                lines.append(
                    f"{event.get('kind') or event.get('event_type')} "
                    f"created_at={event.get('created_at')} "
                    f"id={event.get('event_id') or event.get('id')}"
                )
            else:
                lines.append(str(event))
        if not lines:
            lines = ["no events for this identity"]
        if payload.get("next_cursor"):
            lines.append(f"(next_cursor={payload['next_cursor']})")
        return "\n".join(lines)

    return run_signed(
        ctx,
        render,
        lambda client: client.events(cursor=args.cursor, limit=args.limit),
    )


def cmd_health(args: argparse.Namespace) -> int:
    ctx = context_from(args)

    def render(payload: dict[str, Any]) -> str:
        clock = payload.get("clock") or {}
        return (
            f"status={payload.get('status')} service={payload.get('service')} "
            f"version={payload.get('version')} "
            f"db_skew={clock.get('database_skew_seconds')}s "
            f"(tolerance {clock.get('database_skew_tolerance_seconds')}s)"
        )

    return run_public(ctx, render, lambda client: client.transport.decode(
        client.transport.get("/health")
    ))


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def context_from(args: argparse.Namespace) -> Context:
    return Context(
        base_url=args.base_url,
        identity_path=Path(args.identity).expanduser(),
        timeout=args.timeout,
        json_output=args.json,
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base-url",
        default=os.environ.get("AGENTFORGE_BASE_URL", DEFAULT_BASE_URL),
        help="instance URL (env AGENTFORGE_BASE_URL; default %(default)s)",
    )
    common.add_argument(
        "--identity",
        default=os.environ.get("AGENTFORGE_IDENTITY", DEFAULT_IDENTITY_PATH),
        help="identity file (env AGENTFORGE_IDENTITY; default %(default)s)",
    )
    common.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    common.add_argument(
        "--json", action="store_true", help="print the full server response as JSON"
    )

    parser = argparse.ArgumentParser(
        prog="agentforge-cli",
        description=(
            "Unified operator CLI for AgentForge: one-shot identity, task, claim, "
            "submission, validation, dispute and inspection commands over the "
            "Python SDK. Long-running daemons remain scripts/agent_worker.py "
            "and scripts/validator_worker.py."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_cli_version()}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # identity -------------------------------------------------------------
    identity = subparsers.add_parser(
        "identity", help="create and inspect agent identities (Ed25519 did:key)"
    ).add_subparsers(dest="identity_command", required=True, metavar="ACTION")
    identity_new = identity.add_parser(
        "new",
        parents=[common],
        help="generate a new identity file (atomic, mode 0600)",
    )
    identity_new.add_argument(
        "path", nargs="?", default=None, help="target path (default: --identity)"
    )
    identity_new.add_argument(
        "--force", action="store_true", help="replace an existing identity file"
    )
    identity_new.set_defaults(func=cmd_identity_new)
    identity_show = identity.add_parser(
        "show", parents=[common], help="print the DID for an identity file"
    )
    identity_show.add_argument(
        "path", nargs="?", default=None, help="identity path (default: --identity)"
    )
    identity_show.set_defaults(func=cmd_identity_show)

    # registration and agent views ------------------------------------------
    register = subparsers.add_parser(
        "register", parents=[common], help="register this identity with the instance"
    )
    register.add_argument("--name", required=True)
    register.add_argument(
        "--capabilities", required=True, help="comma-separated capabilities to declare"
    )
    register.add_argument("--chains", default="local", help="comma-separated chains")
    register.set_defaults(func=cmd_register)

    whoami = subparsers.add_parser(
        "whoami", parents=[common], help="show this identity's registered profile"
    )
    whoami.set_defaults(func=cmd_whoami)

    balance = subparsers.add_parser(
        "balance", parents=[common], help="mock-ledger balance for one asset"
    )
    balance.add_argument("--asset", default="MOCK")
    balance.set_defaults(func=cmd_balance)

    reputation = subparsers.add_parser(
        "reputation", parents=[common], help="reputation for this DID (or another)"
    )
    reputation.add_argument("did", nargs="?", default=None)
    reputation.set_defaults(func=cmd_reputation)

    capabilities = subparsers.add_parser(
        "capabilities", parents=[common], help="server capability index (public)"
    )
    capabilities.set_defaults(func=cmd_capabilities)

    agents = subparsers.add_parser("agents", help="agent discovery")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True, metavar="ACTION")
    agents_search = agents_sub.add_parser(
        "search", parents=[common], help="search active agents (public)"
    )
    agents_search.add_argument("--capability", default=None)
    agents_search.add_argument("--chain", default=None)
    agents_search.add_argument("--min-reputation", type=float, default=None)
    agents_search.set_defaults(func=cmd_agents_search)

    # tasks ------------------------------------------------------------------
    tasks = subparsers.add_parser("tasks", help="task posting, discovery and inspection")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", required=True, metavar="ACTION")

    tasks_list = tasks_sub.add_parser(
        "list", parents=[common], help="public task discovery with server-side filters"
    )
    tasks_list.add_argument("--status", default=None)
    tasks_list.add_argument("--kind", default=None)
    tasks_list.add_argument("--strategy", default=None, help="verification_strategy filter")
    tasks_list.add_argument("--capability", default=None)
    tasks_list.add_argument("--chain", default=None)
    tasks_list.add_argument("--origin", default=None)
    tasks_list.add_argument("--min-reward", default=None)
    tasks_list.add_argument("--limit", type=int, default=None)
    tasks_list.add_argument("--cursor", default=None, help="keyset cursor from a previous page")
    tasks_list.add_argument("--offset", type=int, default=None)
    tasks_list.set_defaults(func=cmd_tasks_list)

    tasks_get = tasks_sub.add_parser("get", parents=[common], help="read one task (signed)")
    tasks_get.add_argument("task_id")
    tasks_get.set_defaults(func=cmd_tasks_get)

    tasks_create = tasks_sub.add_parser(
        "create", parents=[common], help="create a task from a JSON file"
    )
    tasks_create.add_argument("file", help="JSON task body, as accepted by POST /api/v1/tasks")
    tasks_create.set_defaults(func=cmd_tasks_create)

    tasks_cancel = tasks_sub.add_parser(
        "cancel", parents=[common], help="cancel one of this DID's open/funded tasks"
    )
    tasks_cancel.add_argument("task_id")
    tasks_cancel.set_defaults(func=cmd_tasks_cancel)

    tasks_submissions = tasks_sub.add_parser(
        "submissions",
        parents=[common],
        help="list a task's submission ids and commitments (signed)",
    )
    tasks_submissions.add_argument("task_id")
    tasks_submissions.add_argument("--limit", type=int, default=None)
    tasks_submissions.set_defaults(func=cmd_tasks_submissions)

    # claims -----------------------------------------------------------------
    claim = subparsers.add_parser("claim", parents=[common], help="claim a task")
    claim.add_argument("task_id")
    claim.set_defaults(func=cmd_claim)

    heartbeat = subparsers.add_parser(
        "heartbeat", parents=[common], help="extend a claim's execution lease"
    )
    heartbeat.add_argument("claim_id")
    heartbeat.set_defaults(func=cmd_heartbeat)

    # submissions ------------------------------------------------------------
    submit = subparsers.add_parser(
        "submit", parents=[common], help="submit a signed proof for a claimed task"
    )
    submit.add_argument("task_id")
    submit.add_argument(
        "--result",
        required=True,
        help="result JSON: a file path or inline JSON starting with '{'",
    )
    submit.add_argument(
        "--evidence", default=None, help="evidence JSON array: file path or inline JSON"
    )
    submit.add_argument(
        "--inference-sessions", default=None, help="comma-separated inference session ids"
    )
    submit.add_argument(
        "--submission-id", default=None, help="pin the submission id (default: generated)"
    )
    submit.set_defaults(func=cmd_submit)

    submission = subparsers.add_parser(
        "submission", parents=[common], help="read one submission (signed)"
    )
    submission.add_argument("submission_id")
    submission.set_defaults(func=cmd_submission_get)

    proof = subparsers.add_parser("proof", parents=[common], help="read one proof (signed)")
    proof.add_argument("submission_id")
    proof.set_defaults(func=cmd_proof_get)

    # validation ---------------------------------------------------------------
    validate = subparsers.add_parser(
        "validate",
        parents=[common],
        help="submit a signed peer-validation decision for a submission",
    )
    validate.add_argument("submission_id")
    validate.add_argument("--decision", required=True, choices=("VERIFIED", "REJECTED", "PARTIAL"))
    validate.add_argument("--checks", default=None, help="checks JSON array: file or inline")
    validate.add_argument("--reason-codes", default=None, help="comma-separated reason codes")
    validate.add_argument("--evidence-hash", default=None)
    validate.add_argument("--policy", default="deterministic_then_domain")
    validate.add_argument("--decision-id", default=None)
    validate.set_defaults(func=cmd_validate)

    validate_task = subparsers.add_parser(
        "validate-task",
        parents=[common],
        help="validate the task's pending submission (server resolves it)",
    )
    validate_task.add_argument("task_id")
    validate_task.add_argument("--submission-id", required=True)
    validate_task.add_argument("--decision", required=True, choices=("VERIFIED", "REJECTED", "PARTIAL"))
    validate_task.add_argument("--checks", default=None)
    validate_task.add_argument("--reason-codes", default=None)
    validate_task.add_argument("--evidence-hash", default=None)
    validate_task.add_argument("--policy", default="deterministic_then_domain")
    validate_task.add_argument("--decision-id", default=None)
    validate_task.set_defaults(func=cmd_validate_task)

    # disputes -----------------------------------------------------------------
    dispute = subparsers.add_parser("dispute", help="escrow disputes")
    dispute_sub = dispute.add_subparsers(dest="dispute_command", required=True, metavar="ACTION")
    dispute_open = dispute_sub.add_parser(
        "open", parents=[common], help="open a dispute on a submission (freezes escrow)"
    )
    dispute_open.add_argument("submission_id")
    dispute_open.add_argument("--reason", required=True)
    dispute_open.add_argument("--dispute-id", default=None)
    dispute_open.set_defaults(func=cmd_dispute_open)

    dispute_resolve = dispute_sub.add_parser(
        "resolve", parents=[common], help="resolve a dispute with a signed decision"
    )
    dispute_resolve.add_argument("dispute_id")
    dispute_resolve.add_argument("--submission-id", required=True)
    dispute_resolve.add_argument(
        "--decision", required=True, choices=("VERIFIED", "REJECTED", "PARTIAL", "SLASHED")
    )
    dispute_resolve.add_argument("--checks", default=None)
    dispute_resolve.add_argument("--reason-codes", default=None)
    dispute_resolve.add_argument("--evidence-hash", default=None)
    dispute_resolve.add_argument("--policy", default="deterministic_then_domain")
    dispute_resolve.add_argument("--decision-id", default=None)
    dispute_resolve.set_defaults(func=cmd_dispute_resolve)

    # events and health ----------------------------------------------------------
    events = subparsers.add_parser(
        "events", parents=[common], help="this DID's signed audit event feed"
    )
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--cursor", default=None)
    events.set_defaults(func=cmd_events)

    health = subparsers.add_parser(
        "health", parents=[common], help="instance health and clock diagnostics (public)"
    )
    health.set_defaults(func=cmd_health)

    return parser


def _cli_version() -> str:
    from . import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except AgentForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except (httpx.HTTPError, OSError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
