# Accounting and lifecycle remediation

**Updated:** 2026-09-18. **Status:** implemented and regression-tested in the working
tree for PR review; independent review pending. Current execution results and PR state belong
in [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md) and
[PROJECT_STATUS.md](PROJECT_STATUS.md), not duplicated here.

## Findings and fixes

| ID | Finding | Implemented control |
|---|---|---|
| AF-L1 | Two helper debits of 60 against 100 both committed, leaving balance 40 despite recorded deltas of −120. | Ledger-key reservation and balance change share the caller's transaction. Balance updates use SQL compare-and-swap against freshly selected strings, with bounded retries; no ORM read/modify/write or SQL float cast. |
| AF-L2 | A schema-accepted `1e-29` debit against 1000 was recorded without decreasing the balance under the default 28-digit Decimal context. | Bounded exact arithmetic in `money.py`, with an independent 200-digit context and inexact/rounding traps. Funding, negation, partial refunds, slash and conservation checks use this arithmetic. |
| AF-L3 | Read-only escrow status checks could let different terminal decision keys authorize competing credits. | Conditional terminal-state update before any credit; stable recipient lock order; rollback restores both escrow and balances if later work fails. |
| AF-T1 | Validation/dispute checks were not serialized, including zero-value tasks without escrow. | SQL submission guard shared by validation and dispute opening. A winning validation closes the associated open dispute in its transaction, including through ordinary `/validate`. |
| AF-T2 | Cancellation used a stale task-status read and could race with claiming. | Conditional task cancellation before refund, with a state-version increment. A concurrent claim/cancel loser cannot overwrite the winner or emit a cancellation event. |
| AF-T3 | Per-executor active-claim counting was not serialized across different tasks. | Executor row guard before task acquisition/counting; concurrent claims cannot exceed the existing limit of ten. |

AF-L1/AF-L2 were originally reproduced by the standalone probe. AF-L3 and the
lifecycle controls were added during the broader transaction review and have
focused competing-operation regressions. This is implementation-side evidence,
not an independent audit or a proof covering every possible interleaving.

## Accounting contract

- Assets remain **MOCK/TEST_CREDIT only**. No real provider billing, token transfer,
  deposit rail or Activity Engine budget controller was added.
- A missing account defaults to zero; concurrent account creation cannot reset an
  existing account. An explicitly enabled development faucet seeds 1000 mock
  credits; production forbids that faucet.
- Replay is accepted only when DID, asset, numeric delta, reason and task reference
  match the existing ledger event. Reusing a key with different content fails.
  Only the intended uniqueness conflict is handled; other integrity errors are
  not converted into successful replay.
- Account writes and event insertion have **no internal commit**. Every caller
  must roll back the whole transaction on failure, including CAS exhaustion,
  insufficient funds or an unrepresentable result. HTTP funding/settlement paths
  do so; tests inject late failures to verify rollback. Retrying only the last
  write is not supported.
- CAS retry exhaustion returns a retryable transaction conflict (HTTP 409 in the
  affected routes). This is not a throughput guarantee; PostgreSQL deadlocks or
  serialization failures outside that CAS still require whole-transaction retry.
- Multi-recipient credits take account locks in stable recipient/key order,
  rather than executor-versus-requester order.

## Precision and compatibility

Storage is still decimal strings of at most **80 characters**, including a minus
sign for ledger deltas. No migration, currency denomination or public API method
was introduced. Nonfinite values, floats/booleans in internal money inputs,
excessive expansion and unrepresentable results fail closed rather than round.
Partial-settlement money must be exact (prefer decimal strings, not JSON floats).

The 200-digit arithmetic context is isolated from the caller's precision and
rounding mode. Supported operands span at most 80 integer/fractional positions;
all operations are sums/subtractions, not arbitrary-precision multiplication.
Exact sign inversion uses `copy_negate()`, which does not inherit ambient rounding.

A task's individual `Money` fields can pass request parsing while their sum,
signed debit or resulting account balance does not fit storage. That funding
request returns 400 and rolls back task, escrow and ledger writes. For example,
`1e-78` cannot be debited from 1000 within the current string width. Do not claim
that every individually accepted decimal can be funded against every balance.

## Coverage and remaining boundaries

`tests/test_accounting_regressions.py` covers both original probes, hostile numeric
inputs, low ambient precision, signed width/overflow, replay conflicts, concurrent
credits/account creation, stale sessions, rollback, every escrow transition,
competing terminal decisions, HTTP overspending, competing validations/disputes,
cancel/claim races and cross-task claim caps.

PostgreSQL-specific tests force a CAS collision and pause cancellation before its
conditional write while a competing HTTP claim commits. Additional PostgreSQL
HTTP tests synchronize both callers before the shared submission guard. These
are concurrency regressions, not load/HA or multi-region tests.

`python scripts/probe_ledger_accounting.py` remains an opt-in disposable-database
check. Its two invariants now pass and are also included in normal pytest
collection, so they can no longer be hidden behind an otherwise green suite.

Automatic refund policy for deadline-expired work, executor collateral, real
inference charges and external settlement remain outside this mock-ledger fix.
Do not use the mock lifecycle as a complete live-money settlement rail.
