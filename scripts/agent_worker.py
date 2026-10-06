#!/usr/bin/env python3
"""Long-running autonomous AgentForge executor.

``simulate_marketplace.py`` drives one scripted exchange and exits. This is the
other half an operator needs: a daemon that stays up, discovers claimable work,
holds its execution lease while the work runs, submits a signed proof, and
shuts down without abandoning anything it had already claimed.

Design decisions worth knowing before you extend this file
----------------------------------------------------------

**One SDK client per thread, never a shared one.** ``Transport`` keeps the
instant a request was signed and the clock offset learned from that response
as transport state. Two threads sharing a transport would interleave those
writes and corrupt each other's drift calibration, so the poll loop, every
job and every lease heartbeat gets its own ``AgentForgeClient`` over the same
``AgentIdentity`` (an identity is just a key: signing is stateless).

**Never claim work this worker cannot finish.** An abandoned lease costs
``-0.1`` executor reputation when the reaper expires it, and a rejected proof
costs ``-1.0``. The worker therefore refuses to claim a task unless it declares
every capability the task requires *and* holds a handler for it. The server
enforces the same capability rule, so claiming blind would only trade a 403
for a reputation loss.

**The lease is heartbeated from the server's own numbers.** A claim response
carries ``received_at`` and ``lease_expires_at`` in server time; the heartbeat
period is a fraction (default one half) of that window measured on the local
monotonic clock. One missed heartbeat therefore still leaves half the window
in hand. The server restarts the lease from its own receipt time for each
heartbeat, so a skewed worker clock can neither extend nor shorten a lease.

**Economic actions are not blindly retried.** The only automatic retry is the
re-signature after a ``ClockDriftError``, where the request was rejected at
authentication and nothing was created server-side. Submissions carry a
pre-generated ``submission_id``, so even that retry is idempotent: a duplicate
is refused with a clean ``409 submission ID already exists`` instead of
creating a second proof.

**Shutdown finishes in-flight work.** SIGINT/SIGTERM stop discovery
immediately, then the worker waits (bounded by ``--shutdown-grace``) for jobs
that already hold a lease to submit their proofs.

Scope: this worker talks to a local or operator-provisioned AgentForge
instance on mock credits. It is not a wallet, it moves no external value, and
it does not make an instance a testnet. See
``docs/EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import random
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from agentforge_sdk import (
    AgentForgeClient,
    AgentForgeError,
    AgentIdentity,
    ClockDriftError,
)

LOGGER = logging.getLogger("agentforge.agent_worker")

#: Task states a claim may legally be attempted from (server: routes/claims.py).
CLAIMABLE_STATUSES: tuple[str, ...] = ("FUNDED", "OPEN")

#: Transport-level failures that justify backing off and polling again rather
#: than terminating the daemon.
TRANSIENT = (httpx.HTTPError, OSError)

#: Claim rejections that are permanent for this (worker, task) pair: a missing
#: capability or a failed independence check will refuse every retry, and a
#: vanished task will not come back. Remembering them is what stops the poll
#: loop re-attempting the same hopeless claim on every cycle.
PERMANENT_CLAIM_REFUSALS: frozenset[int] = frozenset({403, 404})

#: Claim rejections that are only true right now: another worker won the race,
#: the task moved state, or this worker is at the server's active-claim limit.
TRANSIENT_CLAIM_REFUSALS: frozenset[int] = frozenset({409, 429})

#: Upper bound on remembered refusals, so a daemon that runs for weeks against
#: a busy instance cannot grow this set without limit.
MAX_DECLINED_TASKS = 2048

#: ``handler(task, client) -> {"result": {...}, "evidence": [...], ...}``
Handler = Callable[[dict[str, Any], AgentForgeClient], dict[str, Any]]


class LeaseLost(RuntimeError):
    """The execution lease expired or was revoked before the proof was sent."""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def demo_marketplace_handler(task: dict[str, Any], client: AgentForgeClient) -> dict[str, Any]:
    """Satisfy the ``marketplace_demo`` task shape used by the simulation.

    This exists so the quickstart has something that actually settles against
    a fresh development server. It is a fixed answer for a fixed acceptance
    contract, not a general-purpose executor: real deployments pass their own
    callable with ``--handler module:function``.
    """
    return {
        "result": {"answer": "ready"},
        "evidence": [{"kind": "demo_receipt", "content_hash": "sha256:agentforge-ready"}],
    }


#: Built-in handlers keyed by the capability they satisfy.
HANDLER_REGISTRY: dict[str, Handler] = {"marketplace_demo": demo_marketplace_handler}


def load_handler(spec: str) -> Handler:
    """Import ``module.path:callable`` and return it."""
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"handler must be 'module.path:callable', got {spec!r}")
    module = importlib.import_module(module_name)
    handler = getattr(module, attribute, None)
    if not callable(handler):
        raise ValueError(f"handler {spec!r} is not callable")
    return handler


def capability_names(task: dict[str, Any]) -> set[str]:
    """Mirror the server's ``required_capability_names`` for a task view."""
    names: set[str] = set()
    for item in task.get("required_capabilities") or []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class WorkerConfig:
    base_url: str
    capabilities: tuple[str, ...] = ()
    chains: tuple[str, ...] = ("local",)
    name: str = "agentforge-worker"
    poll_interval: float = 5.0
    max_poll_interval: float = 60.0
    jitter_ratio: float = 0.25
    max_concurrency: int = 1
    timeout: float = 30.0
    heartbeat_ratio: float = 0.5
    min_heartbeat_interval: float = 5.0
    heartbeat_interval: float | None = None
    shutdown_grace: float = 60.0
    task_limit: int = 25
    max_tasks: int | None = None
    once: bool = False

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capabilities": list(self.capabilities),
            "chains": list(self.chains),
        }


# ---------------------------------------------------------------------------
# Lease heartbeating
# ---------------------------------------------------------------------------


class LeaseKeeper:
    """Keep one claim's execution lease alive for as long as the handler runs.

    Runs on its own thread with its own client, because the job thread is busy
    doing the actual work and a shared transport is not safe to interleave.
    """

    def __init__(
        self,
        client: AgentForgeClient,
        claim: dict[str, Any],
        *,
        ratio: float = 0.5,
        minimum: float = 5.0,
        override: float | None = None,
    ) -> None:
        self.client = client
        self.claim_id = str(claim["claim_id"])
        window = 0.0
        try:
            window = float(claim["lease_expires_at"]) - float(claim["received_at"])
        except (KeyError, TypeError, ValueError):
            window = 0.0
        if override is not None:
            self.interval = max(float(override), 0.01)
        elif window > 0:
            # Half the server-declared window by default, so a single failed
            # heartbeat still leaves the other half to recover in.
            self.interval = max(min(window * ratio, window), 0.01)
            self.interval = max(self.interval, min(minimum, window * ratio))
        else:
            self.interval = minimum
        self.beats = 0
        self.failures = 0
        self.lease_lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self.claim_id}", daemon=True
        )

    def __enter__(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def _beat(self) -> None:
        try:
            self.client.heartbeat(self.claim_id)
        except ClockDriftError:
            # The transport recalibrated from this response's
            # X-Server-Timestamp; re-signing the same heartbeat now succeeds.
            self.client.heartbeat(self.claim_id)
        self.beats += 1

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._beat()
            except AgentForgeError as exc:
                if getattr(exc, "status_code", None) in {404, 409}:
                    # "claim is no longer active" / reaped: stop beating and
                    # let the job abort rather than submit against a dead lease.
                    self.lease_lost = True
                    LOGGER.warning("lease %s lost: %s", self.claim_id, exc)
                    return
                self.failures += 1
                LOGGER.warning("heartbeat %s failed: %s", self.claim_id, exc)
            except TRANSIENT as exc:
                self.failures += 1
                LOGGER.warning("heartbeat %s transport error: %s", self.claim_id, exc)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class WorkerStats:
    polls: int = 0
    discovered: int = 0
    claims_attempted: int = 0
    claims_won: int = 0
    claim_conflicts: int = 0
    claims_declined: int = 0
    submissions: int = 0
    verified: int = 0
    handler_errors: int = 0
    leases_lost: int = 0
    heartbeats: int = 0
    clock_drift_retries: int = 0
    poll_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class AgentWorker:
    """Autonomous executor: discover, claim, heartbeat, execute, submit."""

    def __init__(
        self,
        identity: AgentIdentity,
        config: WorkerConfig,
        *,
        client_factory: Callable[[], AgentForgeClient] | None = None,
        handlers: dict[str, Handler] | None = None,
    ) -> None:
        self.identity = identity
        self.config = config
        self.handlers: dict[str, Handler] = dict(
            HANDLER_REGISTRY if handlers is None else handlers
        )
        self._client_factory = client_factory
        self._stop = threading.Event()
        self._slots = threading.Semaphore(max(1, config.max_concurrency))
        self._lock = threading.Lock()
        self._futures: list[Future] = []
        # Tasks this worker has been permanently refused, oldest first.
        self._declined: dict[str, str] = {}
        self.stats = WorkerStats()

    # -- plumbing ---------------------------------------------------------

    def new_client(self) -> AgentForgeClient:
        if self._client_factory is not None:
            return self._client_factory()
        return AgentForgeClient(
            self.config.base_url, self.identity, timeout=self.config.timeout
        )

    def request_stop(self) -> None:
        """Stop discovering. In-flight jobs still get to submit their proofs."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _count(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self.stats, field_name, getattr(self.stats, field_name) + amount)

    def decline(self, task_id: str, reason: str) -> None:
        """Remember a permanent refusal so the next poll does not retry it.

        Without this the loop re-attempts a task it can never win on every
        cycle: a 403 for a missing capability or a failed independence check
        is a property of this worker and this task, not a passing condition.
        The map is bounded and evicts oldest-first.
        """
        with self._lock:
            self._declined.pop(task_id, None)
            self._declined[task_id] = reason
            while len(self._declined) > MAX_DECLINED_TASKS:
                self._declined.pop(next(iter(self._declined)))

    def declined(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._declined

    def _retry_on_drift(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one request, re-signing once if the server rejected the clock.

        Safe for every call site here: a drift rejection is a 401 raised before
        the handler ran, so nothing was created and the retry cannot duplicate
        an economic action. The submission path additionally pins its
        ``submission_id`` so even a lost response cannot create two proofs.
        """
        try:
            return operation(*args, **kwargs)
        except ClockDriftError:
            self._count("clock_drift_retries")
            LOGGER.info("re-signing after server clock-drift rejection")
            return operation(*args, **kwargs)

    # -- discovery and selection -----------------------------------------

    def handler_for(self, task: dict[str, Any]) -> Handler | None:
        """Return the handler for a task, or ``None`` if it must be skipped.

        A task is only workable when this worker declares *every* capability
        the task requires -- that is the server's own ``can_execute`` rule --
        and has a handler registered for one of them.
        """
        required = capability_names(task)
        declared = set(self.config.capabilities)
        if required and not required.issubset(declared):
            return None
        for name in sorted(required):
            handler = self.handlers.get(name)
            if handler is not None:
                return handler
        if not required:
            return self.handlers.get("*")
        return None

    def workable(self, task: dict[str, Any]) -> bool:
        if task.get("status") not in CLAIMABLE_STATUSES:
            return False
        if self.declined(str(task.get("id"))):
            return False
        if task.get("poster_did") == self.identity.did:
            # The server refuses a self-claim; do not spend a request on it.
            return False
        deadline = task.get("deadline")
        if deadline is not None and float(deadline) <= time.time():
            return False
        return self.handler_for(task) is not None

    def discover(self, client: AgentForgeClient) -> list[dict[str, Any]]:
        """Public task discovery across claimable states and capabilities."""
        found: dict[str, dict[str, Any]] = {}
        capabilities: tuple[str | None, ...] = self.config.capabilities or (None,)
        for status in CLAIMABLE_STATUSES:
            for capability in capabilities:
                page = client.list_tasks(
                    status=status, capability=capability, limit=self.config.task_limit
                )
                for task in page.get("tasks", []):
                    found.setdefault(str(task["id"]), task)
        self._count("polls")
        self._count("discovered", len(found))
        return [task for task in found.values() if self.workable(task)]

    # -- execution --------------------------------------------------------

    def _run_job(self, task: dict[str, Any], claim: dict[str, Any], handler: Handler) -> None:
        task_id = str(task["id"])
        client = self.new_client()
        heartbeat_client = self.new_client()
        submission_id = f"S_{uuid.uuid4().hex}"
        keeper: LeaseKeeper | None = None
        try:
            keeper = LeaseKeeper(
                heartbeat_client,
                claim,
                ratio=self.config.heartbeat_ratio,
                minimum=self.config.min_heartbeat_interval,
                override=self.config.heartbeat_interval,
            )
            with keeper:
                try:
                    output = handler(task, client)
                except Exception as exc:  # handler code is operator-supplied
                    self._count("handler_errors")
                    LOGGER.exception("handler failed for task %s: %s", task_id, exc)
                    # Deliberately no proof: submitting a known-bad result
                    # costs -1.0 reputation, letting the lease lapse costs
                    # -0.1 and returns the task to the pool for someone else.
                    return
                if keeper.lease_lost:
                    self._count("leases_lost")
                    raise LeaseLost(f"lease for task {task_id} was lost before submission")
                if not isinstance(output, dict) or "result" not in output:
                    self._count("handler_errors")
                    LOGGER.error("handler for task %s returned no 'result' mapping", task_id)
                    return
                submission = self._retry_on_drift(
                    client.submit,
                    task_id,
                    result=output["result"],
                    evidence=output.get("evidence") or [],
                    inference_session_ids=output.get("inference_session_ids") or [],
                    submission_id=submission_id,
                )
            self._count("submissions")
            verification = (submission or {}).get("verification") or {}
            if verification.get("decision") == "VERIFIED":
                self._count("verified")
            LOGGER.info(
                "submitted task=%s submission=%s decision=%s heartbeats=%d",
                task_id,
                submission.get("submission_id"),
                verification.get("decision", "PENDING_REVIEW"),
                keeper.beats,
            )
        except LeaseLost as exc:
            LOGGER.warning("%s", exc)
        except AgentForgeError as exc:
            LOGGER.error("submission failed for task %s: %s", task_id, exc)
        except TRANSIENT as exc:
            LOGGER.error("transport error finishing task %s: %s", task_id, exc)
        finally:
            if keeper is not None:
                self._count("heartbeats", keeper.beats)
            client.close()
            heartbeat_client.close()

    def _claim(self, client: AgentForgeClient, task: dict[str, Any]) -> dict[str, Any] | None:
        task_id = str(task["id"])
        self._count("claims_attempted")
        try:
            claim = self._retry_on_drift(client.claim, task_id)
        except AgentForgeError as exc:
            status = getattr(exc, "status_code", None)
            if status in PERMANENT_CLAIM_REFUSALS:
                # Capability mismatch, failed independence check, or the task
                # is gone. Retrying would fail identically forever.
                self._count("claims_declined")
                self.decline(task_id, str(exc))
                LOGGER.info("not eligible for task %s, will not retry it: %s", task_id, exc)
                return None
            if status in TRANSIENT_CLAIM_REFUSALS:
                # Lost the race or at the active-claim limit; try again later.
                self._count("claim_conflicts")
                LOGGER.debug("claim on %s declined for now: %s", task_id, exc)
                return None
            raise
        self._count("claims_won")
        LOGGER.info("claimed task=%s claim=%s", task_id, claim["claim_id"])
        return claim

    def _dispatch(self, client: AgentForgeClient, pool: ThreadPoolExecutor) -> None:
        for task in self.discover(client):
            if self._stop.is_set() or self._reached_task_limit():
                return
            handler = self.handler_for(task)
            if handler is None:
                continue
            if not self._slots.acquire(blocking=False):
                return  # at concurrency; the next poll picks up the rest
            claim = None
            try:
                claim = self._claim(client, task)
            finally:
                if claim is None:
                    self._slots.release()
            if claim is None:
                continue
            try:
                future = pool.submit(self._run_job, task, claim, handler)
            except RuntimeError:
                self._slots.release()
                return
            future.add_done_callback(lambda _f: self._slots.release())
            self._futures.append(future)
            self._futures = [item for item in self._futures if not item.done()]

    def _reached_task_limit(self) -> bool:
        limit = self.config.max_tasks
        if limit is None:
            return False
        with self._lock:
            return (self.stats.claims_won) >= limit

    def _sleep_for(self, delay: float) -> float:
        """Full-jitter the poll delay so restarted fleets do not synchronize."""
        spread = delay * self.config.jitter_ratio
        return max(0.0, delay + random.uniform(-spread, spread))

    # -- main loop --------------------------------------------------------

    def run(self) -> WorkerStats:
        client = self.new_client()
        pool = ThreadPoolExecutor(
            max_workers=max(1, self.config.max_concurrency),
            thread_name_prefix="agentforge-job",
        )
        delay = self.config.poll_interval
        try:
            while not self._stop.is_set():
                try:
                    self._dispatch(client, pool)
                    delay = self.config.poll_interval
                except AgentForgeError as exc:
                    self._count("poll_errors")
                    delay = min(self.config.max_poll_interval, max(delay * 2, self.config.poll_interval))
                    LOGGER.error("poll failed (%s); next poll in %.1fs", exc, delay)
                except TRANSIENT as exc:
                    self._count("poll_errors")
                    delay = min(self.config.max_poll_interval, max(delay * 2, self.config.poll_interval))
                    LOGGER.error("poll transport error (%s); next poll in %.1fs", exc, delay)
                if self.config.once or self._reached_task_limit():
                    break
                self._stop.wait(self._sleep_for(delay))
        finally:
            self._stop.set()
            self._drain(pool)
            client.close()
        return self.stats

    def _drain(self, pool: ThreadPoolExecutor) -> None:
        """Let claimed work submit its proof, then release the pool."""
        pending = [item for item in self._futures if not item.done()]
        if pending:
            LOGGER.info(
                "waiting up to %.0fs for %d in-flight task(s)",
                self.config.shutdown_grace,
                len(pending),
            )
            wait_futures(pending, timeout=self.config.shutdown_grace)
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Identity, registration and CLI
# ---------------------------------------------------------------------------


def load_or_create_identity(path: Path) -> tuple[AgentIdentity, bool]:
    """Load an identity, or create one written private from its first byte."""
    if path.exists():
        return AgentIdentity.load(path), False
    identity = AgentIdentity.generate()
    identity.save(path)
    return identity, True


def ensure_registered(client: AgentForgeClient, config: WorkerConfig, mode: str) -> None:
    """Register the manifest so the server knows this worker's capabilities."""
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
    parser.add_argument(
        "--identity-path",
        type=Path,
        required=True,
        help="Ed25519 identity file; created with mode 0600 when absent",
    )
    parser.add_argument(
        "--capabilities",
        default="marketplace_demo",
        help="comma-separated capabilities this worker declares and can execute",
    )
    parser.add_argument("--chains", default="local", help="comma-separated chains to declare")
    parser.add_argument("--name", default="agentforge-worker")
    parser.add_argument(
        "--handler",
        action="append",
        default=[],
        help="capability=module.path:callable (repeatable); overrides built-ins",
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-poll-interval", type=float, default=60.0)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--heartbeat-ratio",
        type=float,
        default=0.5,
        help="fraction of the server-declared lease window between heartbeats",
    )
    parser.add_argument("--shutdown-grace", type=float, default=60.0)
    parser.add_argument("--task-limit", type=int, default=25, help="tasks fetched per poll")
    parser.add_argument(
        "--max-tasks", type=int, default=None, help="exit after claiming this many tasks"
    )
    parser.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    parser.add_argument(
        "--register", choices=("auto", "always", "never"), default="auto"
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def build_handlers(specs: list[str]) -> dict[str, Handler]:
    handlers = dict(HANDLER_REGISTRY)
    for spec in specs:
        capability, separator, dotted = spec.partition("=")
        if not separator:
            raise ValueError(f"--handler must be 'capability=module:callable', got {spec!r}")
        handlers[capability.strip()] = load_handler(dotted.strip())
    return handlers


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    config = WorkerConfig(
        base_url=args.base_url,
        capabilities=tuple(item.strip() for item in args.capabilities.split(",") if item.strip()),
        chains=tuple(item.strip() for item in args.chains.split(",") if item.strip()),
        name=args.name,
        poll_interval=args.poll_interval,
        max_poll_interval=args.max_poll_interval,
        max_concurrency=args.max_concurrency,
        timeout=args.timeout,
        heartbeat_ratio=args.heartbeat_ratio,
        shutdown_grace=args.shutdown_grace,
        task_limit=args.task_limit,
        max_tasks=args.max_tasks,
        once=args.once,
    )

    try:
        handlers = build_handlers(args.handler)
        identity, created = load_or_create_identity(args.identity_path)
    except (ValueError, ImportError, AgentForgeError, OSError) as exc:
        print(f"worker startup failed: {exc}", file=sys.stderr)
        return 1
    LOGGER.info(
        "identity %s did=%s", "created" if created else "loaded", identity.did
    )

    worker = AgentWorker(identity, config, handlers=handlers)

    def handle_signal(signum: int, _frame: object) -> None:
        LOGGER.info(
            "%s received; no new claims, finishing in-flight work",
            signal.Signals(signum).name,
        )
        worker.request_stop()

    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(received, handle_signal)
        except ValueError:  # not the main thread (embedded use)
            pass

    registrar = worker.new_client()
    try:
        ensure_registered(registrar, config, args.register)
    except (AgentForgeError, *TRANSIENT) as exc:
        print(f"registration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        registrar.close()

    LOGGER.info(
        "polling %s every %.1fs for capabilities %s",
        config.base_url,
        config.poll_interval,
        ",".join(config.capabilities) or "<any>",
    )
    stats = worker.run()
    print(json.dumps({"did": identity.did, "stats": stats.as_dict()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
