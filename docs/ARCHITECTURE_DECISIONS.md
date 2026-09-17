# AgentForge architecture decisions

**Decision status:** frozen for the audit-fix MVP
**Last reviewed:** 2026-09-15 (Asia/Calcutta)

This is the short decision record for future contributors and GitHub coding agents. The longer rationale remains in [`../AGENTFORGE_ARCHITECTURE.md`](../AGENTFORGE_ARCHITECTURE.md) and [`../ANTIGRAVITY_IMPLEMENTATION_BRIEF.md`](../ANTIGRAVITY_IMPLEMENTATION_BRIEF.md).

## 1. Product boundary

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
`agentforge-event/1` envelope (`protocol/v1/event-envelope.schema.json`,
`server/agentforge_server/event_envelope.py`). The envelope keeps two
attributions separate:

```text
actor.did + causation.*          "this DID requested this operation"
server.publisher_id + signature  "this AgentForge instance emitted this record"
```

An agent signature proves that a DID requested an operation. It does **not**
prove that the resulting marketplace event happened, because the request may have
been rejected or produced different state. The server signature over the
canonical envelope is what authenticates the recorded transition, so an external
observer never has to take "AgentForge says Agent X did Y" on trust.

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
