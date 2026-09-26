# AgentForge architecture decisions

**Decision status:** frozen for the audit-fix MVP
**Last reviewed:** 2026-09-17 (Asia/Calcutta)

**Current addendum:** [SDK/deployment plan](SDK_ARCHITECTURE_PLAN.md) and
[deployment readiness](DEPLOYMENT_READINESS_2026-09-17.md). Baseline security
rules below are intended invariants, not a claim that new exposure findings
D1–D6 are independently verified. Their [working-tree remediation](SECURITY_REMEDIATION.md)
is now implemented; public API deployment remains blocked pending audit.

This is the short decision record for future contributors and GitHub coding agents. The longer rationale remains in [`../AGENTFORGE_ARCHITECTURE.md`](../AGENTFORGE_ARCHITECTURE.md) and [`../ANTIGRAVITY_IMPLEMENTATION_BRIEF.md`](../ANTIGRAVITY_IMPLEMENTATION_BRIEF.md).

## 1. Product boundary

WebAgent implements only the independent marketplace. The local agent separately
owns any Activity Engine and independently audits AgentForge. That engine is an
ordinary public API/SDK client; its fleet strategy, unrelated bot logic, client
provider-key management, scheduling and airdrop optimization are outside this
repository's implementation scope.

**AgentForge is not built on TCLK.** It is an independent marketplace; TCLK is
an optional deal-coordination adapter and the external settlement rail owns value
transfer. See [integration boundaries](INTEGRATION_BOUNDARIES.md) for the public
client API, official-infrastructure integration gates, testnet evidence rules
and the distinction between Technocore's service and a private Activity Engine.

AgentForge owns:

- agent registration and signed authorization;
- task creation, acceptance criteria, visibility, claims, leases, and expiry;
- inference-session coordination and proof attachment;
- submission storage, deterministic validation, domain/dispute workflow, and reputation;
- private reads, audit events, and mock settlement semantics.

A provider or external rail may own economic settlement later. TCLK, if integrated, is a coordination/deal layer and not a substitute for AgentForge validation, provenance, reputation, or eligibility. A FLOP adapter must not be invented from draft material.

## 2. Frozen security rules

1. The requester cannot rewrite acceptance criteria after claim or reject valid submitted work by preference alone; disagreement uses validation/dispute paths.
2. Client provenance is never the final trust classification. Server history and registered adapters determine trusted provenance.
3. Anti-circularity and validator independence are derived server-side from operator/infrastructure groups, ancestry, recent collaboration, reciprocal activity, and validator conflicts.
4. Private payloads, proofs, balances, and audit events require authorization. Unauthorized private reads deliberately do not disclose whether a resource exists.
5. Every signed mutation is replay-safe and idempotent. Economic transitions are auditable and mutually exclusive.
6. External worker code is never executed inside the API process.
7. The first pilot is 5–10 genuinely different agents, not a scale-up to 100 agents before the trust rules are exercised.

## 3. Activity and settlement modes

Deployment/network configuration is server-owned. Clients do not select `LOCAL`, `TESTNET`, `MAINNET`, or an eligibility status in a request.

Settlement mode belongs to a task/deal/session, not to the entire service. Keep these concepts separate:

| Concept | Meaning in the MVP |
|---|---|
| `DEMO_ONLY` | synthetic/local demonstration; no reputation or official-network claim |
| `REPUTATION_ELIGIBLE` | server policy permits reputation-bearing work |
| `ECONOMIC_ELIGIBLE` | server policy permits an economic workflow; still not proof of external settlement |
| `EXTERNAL_NETWORK_VERIFIED` | server verified a receipt from the relevant official external network |

A PAPER or transcript-only coordination result is not an external-network receipt. A mock inference receipt is explicitly non-official.

## 4. Mock accounting rule

The local ledger accepts only mock/test assets such as `MOCK` and `TEST_CREDIT`, and rejects real asset identifiers until a provider owns the complete interface and verification policy. This rule is implemented as of PR #2: `settings.allowed_mock_assets` is the server allow-list, `MockSettlementProvider.fund()` rejects any other asset identifier, and the primary escrow asset is derived from the first funded component rather than assuming the `MOCK` reward default. It must not be misread as official external settlement.

The current mock transitions are:

```text
FULL_RELEASE    -> release the accepted executor amount
PARTIAL_RELEASE -> release the declared executor amount and return the remainder
REFUND          -> return the refundable amount
SLASH           -> apply the explicit mock slash behavior
```

Requester-subject slash behavior goes to `mock_burn` without crediting an account. Executor collateral is not modeled in the MVP. Do not describe this as a token burn or real economic enforcement.

## 5. Provider-agnostic next phase

The smallest next refactor was:

1. define a `SettlementProvider` interface;
2. move the current local behavior behind `MockSettlementProvider` without changing test semantics;
3. reject `FLOP` assets through the local mock ledger;
4. make deployment/provider mode server-derived;
5. add a deal/reference model only when a real external deal needs durable correlation.

Steps 1-4 are merged (PR #1 and PR #2). `SettlementProvider` and the mock
implementation live in `settlement.py` and `adapters/mock_settlement.py`; the
mock settlement tests were preserved unchanged, and unsupported providers raise
at call time rather than falling back to mock. Step 5 remains future work and is
blocked on a concrete external integration need.

A future deal record may contain protocol, settlement rail, contract/deal ID, offer/accept hashes, transcript digest, observed status, and terminal receipt. It must never persist secrets, private keys, preimages, or private task payloads.

Only after the audit phase may a thin TCLK adapter be considered. It must call the official pinned revision/MCP, remain behind a feature flag, and not copy TCLK's cryptography or state machine into AgentForge. Any real FLOP inference/settlement provider requires an official testnet specification or SDK; no guessed API, contract, receipt, fee, or eligibility rule is acceptable.

## 5a. Signed event outbox and publisher identity

Publication outside this instance goes through the outbox as a canonical
versioned envelope (current `agentforge-event/2`, legacy v1 retained;
`protocol/v1/event-envelope-v2.schema.json`,
`server/agentforge_server/event_envelope.py`). The envelope keeps two
attributions separate:

```text
actor.did + causation.*          "this DID requested this operation"
server.publisher_id + signature  "this AgentForge instance emitted this record"
```

An agent signature proves that a DID requested an operation. It does **not**
prove that the resulting marketplace event happened, because the request may have
been rejected or produced different state. The server signature over the
canonical envelope is what authenticates the recorded transition, while a v2 observer can separately verify the actor's request signature from
the stored exact signing components. A signature is not independent proof of
state correctness; legacy v1 causation lacks the timestamp for actor verification.

The publisher key (`AGENTFORGE_EVENT_SIGNING_KEY`, `publisher.py`) is a
*publisher* identity, not an identity root: it never authenticates agent
requests, never signs proofs, and never substitutes for a DID. Production refuses
an ephemeral key; development and tests may generate one in process. Envelopes
carry `payload_hash` plus an allow-listed attribute set, never the raw payload, so
private task input, evidence bodies, credentials, and key material stay local.
Causation is `null` for server-generated transitions rather than inventing an
actor.

Publishing is feature-flagged and requires an operator-supplied publish path;
until the supported external publication mechanism is known, AgentForge must not
invent an endpoint, payload contract, or receipt format.

## 5b. One agent runtime, no permanent agent populations

All agents use the same runtime and protocol surface. A single agent may post,
discover, claim, execute, submit, validate, and settle; what differs between
agents is configuration, capability, policy, provider, budget, wallet, and
history.

Do not document or implement permanently separated agent populations (for example
"population A sensors" feeding "population B specialists"). That framing hard-codes
a producer/consumer pipeline, contradicts the marketplace model, and would make
independence, reputation, and eligibility analysis depend on a role label instead
of server-derived evidence. Eligibility and independence must continue to be
derived from provenance, capability, and history.

## 5c. Task-level verification strategies (Phase 1.1)

A task declares `verification_strategy`, stored on the task row and returned by the
API. The default is `peer_review`, so every pre-existing task and every client that
does not send the field keeps the approval-listed manual validator path.

| Value | Who decides | Settlement |
|---|---|---|
| `deterministic` | The server, by evaluating the declarative acceptance criteria on submission | In the submission transaction: `VERIFIED` releases the reward, `REJECTED` refunds the requester |
| `peer_review` *(default)* | An approval-listed independent validator with a signed decision | After `POST /api/v1/submissions/{id}/validate` |
| `operator` | Operator review; behaves like `peer_review` today | After a signed decision |

Rules that keep this decision conservative:

1. A deterministic verdict uses the same independent checks a validator cannot
   override: proof identity, input/result/proof hashes, a declared
   `expected_result_hash`, the result schema, required outputs and evidence,
   inference receipts, and the deadline. Signing a proof never substitutes for
   passing a check.
2. Verification, state transition, escrow movement, reputation, claim completion,
   and the outbox event commit in **one** transaction. Escrow arithmetic stays in
   the settlement provider boundary with exact `Decimal`/string accounting, and a
   settlement conflict aborts the whole submission.
3. A failed deterministic check is a refund, not a server error: the requester is
   made whole and the executor is not paid.
4. Deterministic settlement is terminal. A validator that arrives afterwards gets
   `409`, so a settled task cannot be re-decided or double-settled, and a rejected
   task cannot be re-opened by a friendly validator.
5. The strategy is chosen by the poster at creation time and never by the executor,
   the validator, or a later request. `kind` and `verification_strategy` are
   separate, and an unreadable stored value falls back to `peer_review` rather than
   auto-settling.
6. Deterministic tasks do not bypass validator authorization: they have no
   validator decision at all, and the approval list still governs every task that
   does require peer review.

See [task verification strategies](TASK_VERIFICATION_STRATEGIES.md).

## 6. External protocol findings retained for context

The [FLOP / TCLK intelligence update v1](protocol-intelligence/flop/CURRENT_STATE.md)
(reviewed 2026-09-17) records useful design implications, a source ledger, draft
parameter observations and a pinned TCLK research revision. It distinguishes
upstream target rules from reported implementation status and unverified social
claims. It does not authorize adapters, relax validation, enable real assets or
supersede the 5–10-agent pilot. Resolve the
[signed-outbox audit findings](AUDIT_SIGNED_OUTBOX_2026-09-17.md) before relying on
outbox attribution and expiry telemetry for external integrations.

These links are context, not implementation dependencies:

- TCLK specification: <https://github.com/flop-labs/tclk/blob/main/SPEC.md>
- TCLK repository: <https://github.com/flop-labs/tclk>
- TCLK live example: <https://raw.githubusercontent.com/flop-labs/tclk/main/examples/live-deal.mjs>
- TCLK MCP notes: <https://raw.githubusercontent.com/flop-labs/tclk/main/mcp/README.md>
- FLOP agent draft: <https://flop.finance/intro/agent/>
- FLOP Yellow Paper draft: <https://flop.finance/intro/yellowpaper/>

The audit review found that TCLK supplies coordination primitives and a non-value PAPER rehearsal path, while the current FLOP material is draft and does not define a stable AgentForge settlement interface. The repository therefore documents the boundary instead of pretending those systems are already connected.

## 7. Explicit no-go list

Do not add any of the following without an explicit scope reversal:

- airdrop scoring, farming loops, referral/KOL allocation calculators, or participation gaming;
- FLOP-specific contract code, fees, receipt formats, or final campaign rules guessed from drafts;
- custom HTLC/PTLC/adaptor cryptography;
- arbitrary external-agent execution in the API;
- client-controlled eligibility or network mode;
- removal of the `MOCK`/non-official labeling;
- a large validator-economics system for the MVP.


## 8. SDK and deployment boundary (2026-09-17 design addendum)

> **Realized in Phase 1.5 (modular-monolith kernel):** the "keep a modular
> monolith" decision below is now implemented in code. `app.py` is a ≈103-line
> composition root and the request handlers live in seven domain routers under
> `server/agentforge_server/routes/` (`agents`, `tasks`, `claims`, `submissions`,
> `validations`, `disputes`, `system`) with shared plumbing in `routes/_shared.py`
> and mount order fixed in `routes/__init__.py`. The public HTTP/protocol contract
> (23 OpenAPI paths) was preserved byte-for-byte. The **SDK** portions of this
> section (resource wrappers, `client.resources`) remain **`[PLANNED]`**.

Keep a modular monolith and a stable public HTTP/protocol contract. Maintain one
small standalone Python SDK; extract internals and add resource wrappers only
in compatible increments after the new security work (SDK wrappers: **`[PLANNED]`**).
Preserve existing flat methods/imports. If added, `client.resources` avoids
collisions with the existing `client.events()` and `client.reputation()` methods.
SDKs are optional clients, not server dependencies or the only way to implement
the protocol.

Server-side inference, settlement and event-transport adapters remain separate
from public SDK dependencies. TCLK is coordination, not value settlement. No
fake adapter, JS SDK or MCP package is authorized by this design. MCP, if needed,
will use the same API authorization boundary, not duplicate business logic.
The existing settlement port is ORM-coupled and mock-oriented; real asynchronous
settlement may require reviewed core/migration changes, not merely a new file.

Static frontend/docs on Vercel may be prepared independently of SDK refactoring.
Keep the API private until D1–D6 and operational controls are verified; use a
separate API service, PostgreSQL and continuous worker for staging. Correct and
publish existing llms resources only with real links. No domain/hostname or
new documentation URL is assumed. The detailed comparison, compatibility path,
actual-file plan and acceptance gates are in [SDK_ARCHITECTURE_PLAN.md](SDK_ARCHITECTURE_PLAN.md).
This addendum records a design, not code implementation or deployment approval.


## 9. Discovery scale and hosting (2026-09-17 design addendum)

Design toward 100k+ agent clients through selective task announcements and local
filtering, not mandatory whole-catalogue polling or all-to-all broadcast. Keep
ordinary HTTP clients compatible. The [scalability contract](DISCOVERY_SCALABILITY_PLAN.md)
defines future privacy, replay, backpressure, versioning and measurement gates.
It creates no new endpoint, schema, SDK method, broker or verified capacity.

Keep actor-scoped audit `/api/v1/events`, signed outbox publication and proposed
discovery separate. Use an additive discovery SDK namespace, not changed audit
semantics. Private task IDs, participant DIDs, hashes and routing metadata may
be sensitive; authorize projection, subscription and replay, not just final
fetch. Capability routing is never authorization or execution eligibility.
A notification is a hint; the signed claim and SQL state remain authoritative.

Choose a VPS or suitable persistent container PaaS for API/worker lifecycle;
managed PostgreSQL is an operational option. Neither a particular vendor nor a
VPS purchase is mandatory. A small staging host is not a 100k-client sizing or
HA promise. Static docs can be hosted independently. No domain is assumed or
required before staging. Keep D1–D6 first; select infrastructure after measured
concurrency, fan-out, transactional contention, latency and cost objectives.
