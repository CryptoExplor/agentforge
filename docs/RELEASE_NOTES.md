# AgentForge pre-testnet MVP release notes

## Unreleased — security/accounting follow-up (2026-09-18)

These are review-branch changes, not a release, merge or deployment announcement.
Current publication state and verification results are maintained in
[PROJECT_STATUS.md](PROJECT_STATUS.md) and
[AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).

### Fixed

- Provider-resolution follow-up: unsupported/invalid settlement configuration now
  fails even after the mock singleton is cached or a test provider is injected.
  Existing `mock`/`local` aliases are unchanged.

- Atomic mock-ledger balance changes and account creation; replay checks compare
  all immutable accounting fields, rather than trusting a reused key.
- Exact bounded money arithmetic across funding, partial refunds, slash and
  conservation; ambient Decimal precision can no longer silently erase debits.
- Competing settlement, validation, dispute, cancel/claim and cross-task claim-limit
  races guarded by transactional SQL writes.
- Bounded mock-inference retention; HTTP retrieval remains authorized and SQL-backed.
- Configuration URL secrets removed from transport/worker status output.
- SDK identity files created privately and replaced atomically, without following
  target symlinks or silently ignoring permission/write failures.
- Dependency advisory remediation in both distributions: `cryptography>=50.0.1,<51`.
- Duplicate status/verification/new-chat context consolidated into canonical docs;
  dated evidence remains explicitly historical.

### Preserved and verified

- Outbox F1–F6 fixes: atomic expiry, full v2 causation, strict envelope schemas,
  publisher preflight, fresh attempts and schema-startup guards.
- D1–D6 controls, including root-only schema dialect declarations, operator grants,
  ingress limits, shared quotas, closed production enrollment and no production faucet.
- Existing public API methods, SDK flat imports/signing formats and envelope
  compatibility. No new migration beyond the existing D1–D6 quota revision.

### Compatibility notes

Money that cannot fit the existing 80-character storage representation fails
closed; partial-settlement floats are rejected. Direct mock-provider cache lookups
may raise `KeyError` after eviction; persisted HTTP sessions remain available.
Transport status exposes only a configured marker instead of a URL.
Identity saves require a trusted directory; failure no longer silently succeeds.
See [accounting policy](ACCOUNTING_REMEDIATION.md) for transaction/retry rules.

## Historical foundation

The baseline/provider work established the neutral marketplace, mock settlement
boundary and server-derived asset/mode guardrails. Actual PR #4 added the signed
outbox foundation; subsequent remediation emits v2 for complete causation and
retains honest v1 compatibility. Dated audit records preserve the original findings
and checkpoint evidence; they are not the current test or merge status.

## Non-goals and gates

MOCK/TEST_CREDIT are not real assets or official FLOP receipts. No Activity Engine,
vLLM adapter, live sensor, real settlement provider, broker, MCP, SDK modularization
or deployment was added. Independent review and human-controlled merge remain
mandatory. Later work follows [PR_PLAN.md](PR_PLAN.md), not an implicit roadmap
execution or a promise of reward eligibility.
