# FLOP / TCLK intelligence update v1

**Reviewed:** 2026-09-17 (Asia/Calcutta)

**Scope:** `[PLANNED]` — documentation and future integration requirements, not implementation approval. Every FLOP/TCLK chain-settlement and Polkadot/FLOP bridging item in this directory is a design horizon; none is wired into code.

**Evidence:** [source ledger](SOURCES.md) · [parameter snapshot](PARAMETER_SNAPSHOT.json) · [change log](CHANGELOG.md)

## Decision summary

Retain the useful architecture guidance from the supplied research, not its
blanket “add adapters now” recommendation. Official draft text can define an
upstream target without proving that a compatible public network or integration
is ready. This document does not change AgentForge's frozen MVP, enable FLOP
assets, or authorize wallet creation, staking, network writes, or fleet rollout.

The Yellow Paper identifies itself as **0.5.0 (draft), implementation spec —
iterating**, with displayed update date **2026-09-05**. Its normative target lives
in the numbered sections and Appendices A/F/G; implementation status lives in
Appendix H and unresolved mechanisms in Appendix E. Read those together, not a
marketing page alone. [S1](SOURCES.md#s1-flop-yellow-paper)

### What to retain, and what already exists

| Useful idea | Existing AgentForge surface | Documentation/design action |
|---|---|---|
| One runtime, roles selected per interaction | Architecture decision §5b; task posting, claims, submission and validation routes | Reinforce capability/policy/budget-based role selection, not permanent sensor/specialist populations |
| Compute is a session with evidence, not just an LLM response | `InferenceProvider`, `InferenceRequestCreate`, persisted `InferenceSession`, mock receipt | Keep request, response, execution evidence, verification and payment outcomes distinct |
| Provider-neutral compute request | `schemas.py` and `providers.py` | Map future external requirements through an adapter; do not duplicate the existing model prematurely |
| Separate compute accounting from settlement | Inference receipt fields versus `SettlementProvider` and mock ledger | Preserve requested/measured/paid/verified quantities and their units; never promote mock work into official network evidence |
| Validation policy depends on task type | `validators/deterministic.py`, evidence and acceptance checks | Record future evaluator requirements; a general `ValidationProvider` is **not implemented** here |
| TCLK is coordination, not settlement | Existing deferred provider strategy | Use a thin pinned integration only after approval; keep rail receipts authoritative for external value |
| Versioned protocol intelligence | This directory | Save dated claims, status, units, source references and unresolved items; no automatic loading from prose |
| Useful interaction telemetry | Audit/outbox events already record task transitions | Propose internal quality metrics, not message-count targets or reward optimization |

Existing implementation references: `server/agentforge_server/providers.py`,
`schemas.py`, `models.py`, `settlement.py`, and `validators/deterministic.py`.
Paths are relative to the repository root. “Existing” is not a claim of complete
correctness: the [signed-outbox audit](../../AUDIT_SIGNED_OUTBOX_2026-09-17.md)
contains unresolved findings and remains applicable.

## 1. Role flexibility without confusing trust domains

A generic runtime may act as requester, executor, researcher, compute consumer,
or task evaluator according to its capabilities and policy. It cannot evaluate
its own work merely by switching role labels. Preserve operator/infrastructure
conflict checks and server-derived provenance/independence.

**FLOP network validator is not AgentForge task evaluator.** FLOP specifies a
staked consensus/DA/attestation role; the target separates inference execution
from validator duties. Appendix H also says the separate checker lane is planned
and the shipped checker currently re-prefills inside the validator watchtower.
Do not turn the target “validators never execute inference” into a blanket
statement about current implementation. [S1](SOURCES.md#s1-flop-yellow-paper),
[S4](SOURCES.md#s4-validator-role)

FLOP mining is a separate hardware, model-serving, calibration, proof-generation,
stake and availability responsibility, not a task-role toggle. Ordinary-GPU SOFT
participation is a target, while the miner page and Appendix H explicitly mark
its end-to-end settlement lane as planned. No miner runtime is authorized by
this update. [S3](SOURCES.md#s3-miner-role)

The **5–10-agent pilot** in [architecture decisions](../../ARCHITECTURE_DECISIONS.md)
remains the rollout boundary. A 200-agent fleet is a planning scenario, not an
approved scale-up, and 200 keys do not establish 200 independent operators.

## 2. Future compute request and evidence mapping

The agent overview lists model-weight hash, maximum latency, requested FLOPs,
confidentiality flag and fee. That is a useful requirements checklist, **not a
complete wire schema**: Yellow Paper Appendix G's compute-channel interfaces
also bind miner, roots, decode policy, keys, SLA, escrow, nonce and other terms.
[S2](SOURCES.md#s2-agent-session-overview), [S1](SOURCES.md#s1-flop-yellow-paper)

Do not implement the pasted `fee_flop: int` model or create `v0_5/current`
packages now. A floating “current” dependency is not a reproducible version pin.
A future approved adapter design should account for:

| Concern | Proposed requirement, not a new public API |
|---|---|
| Correlation | Stable request/session/task IDs; authenticated requester; external account binding verified separately from AgentForge DID |
| Model identity | Algorithm-qualified model commitment plus resolved provider reference; a model nickname is not a weight hash |
| Compute | Exact quantity plus explicit unit and provenance; FLOPs, token counts, FLOP currency and `G_n` are different quantities |
| Cost | Exact decimal-string amount, asset, unit/scale, quote expiry and bounded budget; convert to chain base units only in the pinned adapter |
| SLA/privacy | Latency unit, requested confidentiality and verified assurance tier; requesting confidentiality is not proof of it |
| Execution evidence | Request/result commitments, provider/session/model bindings and receipt reference; sensitive I/O stays access-controlled |
| Verification | Verifier identity, evidence hash, policy/version, scope, outcome and time; distinguish execution integrity from answer quality |
| Settlement | Rail/network identity, independently verified receipt/finality and replay protection; no payment claim from a transcript alone |
| Compatibility | Upstream commit/release, runtime/network identity, supported wire versions, capabilities and tested conformance vectors |

Preserve logical separation:

```text
compute requested -> result received -> execution evidence checked
                                   -> task acceptance evaluated
                                   -> external payment independently verified
```

These are separate facts, not necessarily one linear state machine or four new
SQL tables. Provider output from NVIDIA/OpenAI/etc. is not automatically FLOP
PoUI evidence. The current mock receipt stays `MOCK_VERIFIED` with no official
network receipt; this update does not change that behavior.

## 3. Validation: exact commitments, task-appropriate quality checks

Keep exact comparison for signatures, hashes, canonical encodings and tasks that
explicitly require deterministic equality. For probabilistic output, a future
approved policy may combine schema checks, immutable acceptance criteria,
evidence and independent evaluation. Do not require two valid LLM answers to be
byte-identical, but do not replace cryptographic checks with vague similarity.

FLOP's draft verification design discusses activation commitments, sampled
checks and disputes. This is **execution-integrity verification**, not a generic
quality oracle for AgentForge answers. The current verification page describes
checking slices/turns rather than necessarily re-running an entire session;
do not save “full re-execution after every flag” as a universal rule.
[S5](SOURCES.md#s5-verification-design)

A future evaluator boundary needs its own approved scope, calibration and
negative tests, independent trust domains, conflict handling and dispute
semantics. Do not relax today's deterministic validator based on a tweet.

## 4. TCLK: preserve the integration and privacy boundaries

Research pin: **`5cc4ab93efbc8999a3a7e1471b639deca25998ea`**. This is the upstream
revision inspected for documentation, **not** an installed dependency or an
endorsed production version. [S7](SOURCES.md#s7-tclk-specification),
[S8](SOURCES.md#s8-tclk-changelog)

The useful separation is:

```text
AgentForge task/evidence policy
  -> future DealReference (opaque IDs, commitments and observed status)
  -> approved pinned TCLK client/MCP integration
  -> separately configured settlement rail + verified external receipt
```

Key requirements to save from the pinned specification:

- TCLK is a convention/client layer; it neither proves useful task completion nor
  replaces the rail as the source of truth for money.
- Treat deal transcripts as **public**, even if a room is unlisted. Do not place
  private task input, credentials, payment keys or secret witnesses in AgentForge
  outbox envelopes or deal-reference records. Any future protocol reveal belongs
  inside the separately approved adapter's secret-lifecycle design.
- Capability notes and CAS state pointers are routing hints, not trusted state.
  A valid signed frame proves authorship, not that funds are locked.
- Verify full records, not detached text: room, exact line, sender, nonce and
  signature must stay bound. Venue timestamp/sequence are explicitly **not
  sender-signed**; retain their provenance and reject missing timestamps rather
  than substituting the verifier's wall clock.
- Preserve exact decimal-string nonces above JavaScript's safe integer range.
  Do not round identifiers through floats.
- Persist required evidence independently of ephemeral room retention. Sequence
  numbers can restart; they are not sufficient global deduplication IDs. A full
  transcript must not silently omit malformed records and claim completeness.
- Check rail matching, frame/schema parity, deadline boundaries, replay and
  receipt consistency against the pinned upstream conformance behavior.

The changelog records schema-owned fields, rail matching, safer nonces, record
folding and heartbeat changes under **Unreleased**. Its **0.1.0 (2026-09-01)**
release entry explicitly says alpha/testnet only, no value-holding rail in that
release, and unaudited reference adaptor cryptography. Do not conflate those
sections, assume package `0.1.0` contains Unreleased changes, or copy its crypto.
[S8](SOURCES.md#s8-tclk-changelog)

AgentForge's signed event envelope is **not** a TCLK frame. The configured gossip
publisher must not be pointed at a TCLK/Technocore endpoint on the assumption
that JSON formats, signatures or receipt meanings are interchangeable. No remote
endpoint or deployment was exercised for this research.

## 5. Parameters: source observations, not runtime defaults

The [JSON snapshot](PARAMETER_SNAPSHOT.json) records the draft values discussed
in the supplied text, with sources, units and `runtime_value: null`. It is
**documentation only** and not imported by the application.

- Agent identity stake: **10 FLOP** in Appendix A.
- Session-key circuit breaker: **100 transactions / 250 FLOP / 60 blocks** in
  Appendix A; the window is in the reference-only parameter group.
- Miner base self-stake: **10,000 FLOP**, plus a capacity-proportional bond; the
  miner page warns the rate is under economic reconciliation.

These values were confirmed **in the draft source**, not on a live network.
[S1](SOURCES.md#s1-flop-yellow-paper), [S3](SOURCES.md#s3-miner-role)

A future parameter loader must be separately scoped: bind values to network and
runtime version plus a finalized observation, validate units/ranges/freshness,
record origin, and refuse relevant economic actions when required values are
unknown. Never scrape prose into live spending limits or silently fall back to
this research snapshot. A block window is not a guaranteed wall-clock duration.

## 6. Telemetry and honest operator metadata

Useful proposed internal metrics: response rate/latency, claimed-to-completed
conversion, acceptance/dispute outcomes, verified evidence coverage and useful
follow-through. Define observation windows and denominators before comparing
rates; exclude retries, duplicate events and own-fleet loops as independent
interactions. The outbox audit's duplicate-reaper and attribution findings must
be fixed before relying on those events for such metrics.

`operator_group` and `infrastructure_group` already exist. Additional
funding/deployment/credential-group correlations require a privacy-reviewed,
server-controlled design. Store opaque grouping IDs, never actual credentials;
self-reported differences do not establish independence. Keep this sensitive
metadata out of public gossip. Common ownership must not be hidden with proxy,
user-agent or timing tricks.

The supplied probe experiment is a useful **hypothesis** for internal telemetry,
not a verified network SLA or incentive rule. No original post URL was provided
for the claimed 120-second response window, ecosystem counts, voting warning or
referral announcement. Preserve their uncertainty in the source ledger, not as
runtime requirements. GPU price observation is optional infrastructure research,
not an automatic miner deployment or procurement instruction.

## 7. Add now / defer / exclude

**Add now (documentation only):** this source ledger and parameter snapshot,
role/trust-domain distinctions, evidence/accounting boundaries, proposed
telemetry definitions, future conformance checklist and explicit unknowns.

**Defer to a separately approved implementation:** live FLOP RPC/wallet/session
keys; a real compute/verification/settlement adapter; TCLK integration and
DealReference storage; parameter discovery; generalized quality evaluators;
miner runtime; larger fleet deployment. Gates include a published supported
interface and implementation-status review, pinned dependency and vectors,
network identity/finality/receipt checks, custody and privacy review, budget
controls, failure/replay tests and an audited pilot. A testnet announcement by
itself is not sufficient approval. The FLOP starter remains quarantined.

**Exclude from AgentForge scope:** airdrop formulas, token-conversion or reward
predictions, activity multipliers, referral/KOL farming or analytics modules,
proxy/UA evasion, and treating multiple identities as independent operators.

## 8. Maintenance rule

For every future announcement, record the source/date/version, exact claim,
classification, upstream implementation status, local verification evidence,
and resulting decision. If sources conflict, log the conflict and keep the
integration disabled until resolved. An upstream label `LIVE` means the source
reports implementation; it is not our evidence of a compatible reachable public
network. See [SOURCES.md](SOURCES.md) for classification and reviewed coverage.

This checkout has no `implementation_plan_flop.md`; this is the canonical local
handoff instead of inventing or editing a plan in another agent's repository.
No merge authority or audit finding is changed by this intelligence update.
