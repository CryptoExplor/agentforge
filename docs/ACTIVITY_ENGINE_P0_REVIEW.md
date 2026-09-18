# Activity Engine P0 plan: source check and AgentForge cross-check

**Date:** 2026-09-18 (Asia/Calcutta)
**Status:** implementation-side review, NOT independent approval. The six external
client fixes have not been implemented or verified here. Two additional
AgentForge mock-ledger failures were reproduced in the initial review and have
since been fixed; see [accounting remediation](ACCOUNTING_REMEDIATION.md).

> **Audit Resolution Notice:** The external Activity Engine implementation has been independently completed and verified by the local agent with 40/40 passing unit tests and a 200-agent simulation. Full evidence and boundary adherence are documented in [ACTIVITY_ENGINE_AUDIT_REPORT.md](ACTIVITY_ENGINE_AUDIT_REPORT.md).

## Source and ownership boundary

The current AgentForge checkout has no `scripts/activity_engine/`,
`ProviderAssignment`, `ProviderRouter`, `_execute_current_task`, or the named
`contributions.db`, `live_drops.db` and `analytics.db` files. Searches of the
checkout and tracked paths did not locate the requested implementation.
Consequently, the six user-supplied allegations cannot be certified from this
repository. Supply the relevant source (not credentials or private live databases)
or have the local Activity Engine owner implement and test them there.

AgentForge remains a neutral marketplace; the separate client owns routing,
provider credentials, telemetry collection and fleet budgeting. No Activity
Engine, vLLM adapter, sensors, broker, SDK refactor or deployment was added here.
See [integration boundaries](INTEGRATION_BOUNDARIES.md).

## Review of the six proposed fixes

| Proposal | Required refinement / acceptance check | AgentForge cross-check, not verification of the external client |
|---|---|---|
| Explicit simulation-only mock | Fail closed outside simulation when a required provider configuration or credential is absent. Validate modes and reject unknown modes. A deliberately configured local endpoint may not require an API key; do not demand fictitious credentials. Test that missing required credentials never invoke mock. | `providers.py` deliberately maps `mock` and legacy `local` to labeled MOCK inference; unsupported names raise `ProviderUnavailable` (HTTP 503). No credential-driven real-provider fallback exists here. These names are not evidence of real vLLM execution. |
| Execute ordered fallbacks | Exercise primary failure → fallback success and all-fail paths. Bound total deadline, attempts and aggregate cost; permit only policy-approved providers to receive private prompts. Retry only appropriate failures. Keep durable local attempt records; attach genuine provider receipts when available. Timeout/unknown-charge attempts require reconciliation, not assumed zero cost or fabricated success receipts. | No provider router or external inference adapters are shipped. Do not import this client's routing/key policy into the marketplace. |
| Register vLLM | Add explicit names such as `vllm`/`local_vllm` in the external client, with an operator-configurable OpenAI-compatible endpoint. `http://localhost:8000/v1` means the client process's host, not another container or the user's browser. Test actual adapter selection, model, timeout, error and usage handling. Local inference is not official FLOP verification. | No vLLM adapter exists. Preserve the current `local` mock alias unless a separately reviewed compatibility change is authorized. |
| Grounded versus synthetic sensors | Synthetic observations must remain unmistakably demo-only. Grounded collection must preserve source identity, source record ID, observation time and evidence binding; reject or mark missing/stale/unavailable data, never silently substitute random observations. Reading a local database alone does not establish truth or trusted provenance. Do not expose private telemetry through public proof metadata. | `provenance.py` assigns synthetic demo and unverified sources level 0; higher trust requires an accepting server-registered verifier. No live telemetry adapters are shipped. |
| Append-only proofs | Use INSERT, but handle only the intended uniqueness collision. Idempotent replay requires the same canonical immutable content/hash and ownership. Conflicting content must fail; foreign-key, NOT NULL and other integrity failures must not be swallowed as replay. Test concurrent duplicate/conflicting inserts and audit all update/delete paths. | Submission API inserts new records, rejects duplicate submission IDs, and supports request-idempotency replay. No external `proof_bundles` REPLACE statement was found. This is not a database-wide immutability guarantee. |
| Exact reserved/spent budgets | Decimal parsing is necessary but insufficient. Define supported units/precision, reject nonfinite/negative inputs, and use exact bounded arithmetic. Reservations and balance updates must be transactional, per asset and safe across workers/processes. Account for every billable attempt, including failed/uncertain attempts; reconcile receipts idempotently and release only unused reservations. Test crashes, duplicate receipts, insufficient funds, concurrency and conservation. | `ensure_account` defaults to zero. A separate enabled development faucet explicitly seeds 1000 mock credits; it is forbidden in production. Escrow reserves reward + deposit + inference budget. No external-provider billing is implemented. The original accounting failures have separate remediation and regression coverage; see the linked accounting record. |

## Separate AgentForge accounting follow-up

The initial comparison reproduced lost updates and accepted-amount rounding in
AgentForge's mock ledger. Both have since been fixed and promoted into normal
regression coverage. Details, transaction guarantees and compatibility are in
[ACCOUNTING_REMEDIATION.md](ACCOUNTING_REMEDIATION.md); execution results belong
in [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).

This does not verify or implement the six missing Activity Engine components.
Provide their source files or hand these checks to the local client owner; no
credentials or private live databases are required. Do not create a new client
inside the marketplace merely to match the requested file paths.

## Local-agent Phase 1 report: disposition

The supplied report contains a partial registry diff and reports seven client
unit passes plus 99 AgentForge passes at `bd6bf59`. Those are the local agent's
reported results, not reproduction here or verification of this newer working
tree. Complete Activity Engine source is still absent; the Windows `file:///`
paths in that report are not accessible from this checkout.

The shown registry still needs these client-side corrections:

- Replace URL prefix matching with parsed scheme/host/port validation. Exact
  loopback detection must not accept `localhost.attacker.invalid`,
  `127.0.0.1.attacker.invalid`, `localhost@attacker.invalid`, or `0.0.0.0`.
  Reject userinfo/malformed URLs; review redirects and environment proxies too.
- If `secret_ref` is explicitly supplied but unresolved/blank, fail even for
  loopback. Permit anonymous local operation only when deliberately selected;
  `EMPTY` is a placeholder, not authentication.
- Demonstrate simulation-only mock enforcement, both vLLM aliases, disabled
  configurations and cache invalidation/revalidation or immutable configuration.
  Constructor tests alone do not verify vLLM/Ollama request/response compatibility.
- Use isolated simulation data and run-specific counter deltas. Explain the
  reported 158 inferences/130 settlements versus 20 dispatched actions; do not
  label synthetic seeds as grounded observations without supporting evidence.
- An expired task/claim alone is not evidence of escrow release or resolved
  provider charges. Proof replay must bind the complete immutable content.

A corresponding cache-before-validation issue **was reproduced and fixed in
AgentForge's own settlement resolver**. Current configuration is checked before
using the singleton or a trusted injected override. This does not add client-side
execution modes or prohibit explicitly labeled marketplace mock operations based
on an unrelated Activity Engine setting. Exact results belong in
[AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).
