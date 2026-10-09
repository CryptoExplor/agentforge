# Chain-agnostic settlement attempts (Phase 2.5)

## Scope and activation boundary

This implements an **internal durable command queue and reconciliation worker**,
not an external settlement provider. No HTTP endpoint, signing format, packaged
protocol schema, SDK method, mock payout, provider name, setting or deployment
service changes. `SettlementProvider.fund()` / `.settle()` remain synchronous
SQL-only mock operations. They do **not** enqueue external work: doing both would
risk a mock payout followed by a second, external economic action.

There is no RPC implementation, real signer, concrete receipt verifier, chain
configuration, testnet evidence or deployment authorization. Mock credits remain
valueless database rows. Enabling a rail still requires all evidence and review
in [the external adapter intake gate](EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md).
Current verification lives only in [project status](PROJECT_STATUS.md) and
[audit verification](AUDIT_VERIFICATION.md).

## Durable records and transaction ownership

- `settlement_intents`: immutable canonical unsigned `IntentSpec`, SHA-256
  commitment, task/payer binding, operator-pinned opaque target and idempotency
  key. Unique `(task_id, slot)` reserves **one hold and one terminal allocation**
  per task even if a caller changes keys or chooses a conflicting terminal action.
- `settlement_attempts`: numbered attempt, state, transaction reference, submission
  start marker, fenced lease, next observation time, block/hash reference,
  finality depth, verification count and fixed error code. A partial unique index
  permits only one non-failed/non-replaced attempt per intent. State/counter/lease
  checks are database backstops on both supported dialects.
- `settlement_attempt_events`: append-only local journal of enqueue, dispatch,
  reconciliation, verified observations, replacements and review-required events.
  Records contain bounded identifiers/commitments and fixed codes, not raw
  receipts, arbitrary exception messages or keys. There is no journal-edit API.

`enqueue_intent(session, spec, idempotency_key=...)` is a trusted **in-process**
producer interface, not client authority. It flushes but never commits, opens a
savepoint, calls a port or owns the caller's transaction. The intent row itself
is the transactional work outbox; it does not need a second delivery queue.
Caller business changes, initial attempt and journal commit or roll back together.
On any error, the caller must roll back the **whole** transaction.

Exact key/content replays return the original intent without another attempt,
including after finalization/failure. Money strings normalize to exact decimal
values before commitment. A different commitment under the same key or another
key for the same economic slot fails closed. A terminal intent requires a
matching, independently finalized hold (target, asset, payer, reserved amount and
finality policy); the worker checks the hold again before initial dispatch.
That SQL guard is serialized with hold reconciliation, not held over network I/O.
A later reorg remains possible; the eventual rail must enforce its own escrow
and unique-intent conditions at execution time.

`IntentSpec` supports hold, release (including partial release), refund and slash:

- hold disburses nothing;
- terminal allocations satisfy `released + fee + refunded + slashed == reserved`;
- amounts are bounded, nonnegative exact decimal strings, never floats;
- releases/fees require named recipients; release cannot slash; refunds/slashes
  cannot pay a release or charge a fee; and
- zero-value work needs no external command.

This is **not** a new fee policy or authorization mechanism. Future producers
must derive authorized outcomes, assets, recipients and fees from marketplace
state using the existing policy, not from client claims. One terminal allocation
is deliberately the limit: split executions, automatic amendments and retries of
a failed economic action are unsupported. Incomplete/mismatched execution cannot
be labeled finalized by this engine.

## Worker and independent verification ports

`SettlementWorker(session_factory, target=..., submitter=..., verifier=...).tick()`
is a bounded, dormant library entry point. A future explicitly approved scheduler
must supply trusted ports and a dedicated session factory. There are no default
ports, runtime plugin imports or environment variable that bypass the gate.
The existing reaper/publishing worker is unchanged.

A tick selects only its configured target, claims at most its bounded limit and:

1. Conditionally acquires a unique lease token and writes the dispatch-started
   marker (or starts read-only reconciliation) in a short transaction.
2. Commits and **closes the session**, passing only frozen values to the port.
3. Calls `Submitter.submit(Command)` at most once per initial intent. This is where
   a future approved rail would do RPC or signer I/O, strictly after commit.
4. Opens a fresh transaction to record a reference only if the same unexpired
   lease still owns the attempt. A reference is **not** confirmation or finality.
5. On a later tick, calls the separate `ReceiptVerifier.inspect(Command, ref)`
   outside any session. A missing ref requires read-only lookup by unique intent ID.
6. Rechecks the returned commitments and records the observation and transition
   atomically under the lease fence.

A recovered lease never causes a second submission. `submission_started_at` is
committed *before* the send and never cleared. A crash immediately before the
send is indistinguishable from a crash after the rail accepted it; either leads
to reconciliation, not resubmission. Exactly-once economics is **not** claimed:
the eventual rail must deduplicate by immutable intent ID, and its submitter must
not disguise unsafe resubmission inside internal retries.

Ports must enforce bounded I/O timeouts. A hung call can lose its lease; its late
result cannot overwrite a successor, even if no successor has yet written a new
result. Only the owner of an **unexpired** lease may persist results. A worker
crash or database write failure leaves a recoverable durable marker/lease.

### What “independent receipt verification” means here

The two protocols/types separate submission from verification. A submitter can
return only a `SubmissionReference`; `success=true` and submission responses
cannot be used as verified observations. Passing the same object as both ports
is rejected as a wiring guard, **not a sandbox or a security proof**.

`VerifiedReceipt` is a typed assertion from trusted verifier code; constructing
one is **not cryptographic verification**. No concrete verifier ships in this
phase. An approved implementation must fetch/authenticate independent evidence,
check pinned domain/rail and escrow identity, event signatures/topics or equivalent,
asset, amounts, parties, unique intent ID, transaction, canonical block and finality.
It must never perform signing or economic writes. It must not trust the
submitter's success claim or simply echo the command as observed facts.

Core additionally checks intent ID/hash and the **entire** committed spec,
transaction identity, bounded references/counters, block evidence and the
committed minimum finality depth. Both FINALIZED and definitive FAILED require
finality evidence. A wrong recipient, fee, partial amount, target, asset or hash,
an unknown observation or unavailable verifier cannot finalize an attempt.

## States, replacement and reconciliation

| State | Meaning / next action |
|---|---|
| `PENDING` | Committed, not yet dispatched. Claim commits the submission marker before any send. |
| `SUBMITTED` | Only a transaction reference is known; poll the independent verifier. |
| `CONFIRMED` | Independently observed inclusion; continue waiting for required finality. |
| `FINALIZED` | Exact committed outcome independently observed at required depth. Continue read-only observation for deep reorgs. |
| `FAILED` | Independently verified definitive failure with finality evidence; stop this attempt. No automatic resubmission/new intent. |
| `REPLACED` | Independent verifier attested another reference for the same intent. Old attempt stops; a numbered `SUBMITTED` successor is created atomically. No second send. |
| `REORGED` | Canonicality changed (or a finalized observation regressed to submitted/confirmed). Reconcile; never submit a new action. |
| `MANUAL_REVIEW` | Ambiguity, invalid/unavailable evidence, corrupt command or lost hold finality. Continue read-only reconciliation with capped backoff; never automatically send. |

Replacement references must differ from the prior reference and cannot cycle
through an already-recorded transaction. The chain of replacements is bounded;
rejected replacements enter manual review. Every successor must independently
verify the same economic intent. A post-finality reorg does **not** silently undo
mock balances, erase a finality event or dispatch compensating transfers: this
engine has no ledger/payout callback. Eventual marketplace finalization and
compensation policy require a separate reviewed integration.

Regular observations default to 30-second polling; uncertainty backs off to a
one-hour cap. Finalized attempts keep being observed, which has an operational
cost to budget before enabling a real verifier. FAILED/REPLACED attempts are not
polled; their slots/history remain reserved. A verifier's `UNKNOWN`/not-found is
never interpreted as proof that an earlier send did not occur.

## Operations and manual recovery

There is deliberately no public retry/reset/delete action. A future operator
must monitor aged pending attempts, manual-review/reorg states, lease age,
verification backlog and balance drift under the intake gate before deployment.
No new metrics HTTP endpoint is added in this phase.

For an ambiguous outcome: preserve journal and immutable intent, pause future
producer activity if needed, investigate independently by intent ID and known
transaction references, and restore the approved read-only verifier. Valid
matching evidence may resolve manual review automatically. If no decisive
evidence exists, leave the record unresolved; **do not clear the submission
marker, change keys, edit the journal, or submit another economic action**.
There is no force-finalize escape hatch or automatic compensating transfer.
Operational decisions not resolvable by verified observations require a future
reviewed recovery procedure, not ad-hoc database edits.

## Migration and rollback

The additive migration creates only the three new tables and their constraints;
it neither alters existing escrow semantics nor backfills/enqueues mock tasks.
Startup requires the new schema head even while this machinery is dormant.
Use `alembic upgrade head` before deploying the new application.

Prefer application rollback retaining the additive schema. The old application's
production migration-head guard also requires its exact revision, so application
rollback alone needs an explicitly reviewed compatibility plan; do not assume an
old binary will start against a newer stamped database. Before schema downgrade,
stop all producers/workers, take and verify a private backup, archive attempt
history and obtain human authorization. Downgrade to the preceding revision
**destroys the new intent/attempt/journal records** but preserves existing
marketplace tables. Test the rollback on a disposable restored database first.

## Explicit non-goals

No live RPC, chain-specific interfaces, adapter enablement, real assets or keys;
no mock-to-external dual writes; no new API/error/projection/metrics contract;
no public testnet/pilot deployment; no subscriptions, broker, fleet, inference
marketplace, airdrop, tokenomics or FLOP coupling; no claims of independent audit,
exactly-once external effects, measured scale or production readiness.
