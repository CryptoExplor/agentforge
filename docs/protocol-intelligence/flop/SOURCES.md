# Source ledger — FLOP intelligence v1

**Retrieval/review date:** 2026-09-17 (Asia/Calcutta).
**Method:** read-only official web pages and pinned upstream documents; GitHub
API used only to resolve the public TCLK `main` revision. No upstream code,
conformance suite, live chain, MCP deployment or transport was executed.

This ledger stores summaries and links, not full copies of third-party sources.
FLOP pages are mutable: displayed version/update date plus retrieval date are
not an immutable archive. No content hash or archival copy is claimed. Recheck
before implementation; capture a licensed immutable revision or reviewed source
artifact when selecting an actual dependency. The TCLK links below are
commit-specific and are a research pin only.

## Classification

Record **claim class**, **source verification**, and **implementation evidence**
separately. Do not use “confirmed protocol” to mean “production ready.”

| Claim class | Interpretation |
|---|---|
| `DRAFT_SPEC` | Normative upstream target, still draft; not necessarily implemented |
| `IMPLEMENTATION_STATUS` | Upstream reports built/partial/planned behavior; not independently executed here |
| `VERSIONED_PROTOCOL` | Rules stated at a specific protocol/repository revision; local conformance not implied |
| `ECOSYSTEM_SIGNAL` | Potential research context, not an API or incentive contract |
| `MARKETING_STATEMENT` | Narrative/aggregate claims, never a runtime input |
| `UNRESOLVED` | Missing primary evidence, ambiguous or contradictory |

`SOURCE_VERIFIED` means the cited text was read and supports the summary.
`USER_SUPPLIED_UNVERIFIED` means only the pasted research provides the claim.
No entry here has independently verified live-network evidence.

## S1: FLOP Yellow Paper

- URL: <https://flop.finance/intro/yellowpaper/>
- Displayed version: **0.5.0 (draft)**; **Implementation spec — iterating**.
- Displayed update date: **2026-09-05** (not an immutable commit date).
- Class: `DRAFT_SPEC` plus `IMPLEMENTATION_STATUS`; `SOURCE_VERIFIED`.
- Reviewed coverage relevant to this update: version/read guide §0; architecture
  and role glossary §0.1/§1.1; Appendix A agent/session-key and reference-only
  circuit-breaker window rows; Appendix F.4–F.6; Appendix G compute-channel
  interfaces; Appendix H.1–H.4 status matrix. This was **not** a complete
  line-by-line review of the entire Yellow Paper.
- Useful distinctions: normative sections/A/F/G versus informative implementation
  status H/open questions E; parameters are generated from the upstream parameter
  file, not inferred from inline examples; validator target versus checker
  implementation; requested compute versus exact metering units.
- Source-reported gaps: SOFT settlement end-to-end `PLANNED/TBD`; TOPLOC and
  re-execution/dispute coverage `PARTIAL`; separate checker lane `PLANNED` with
  current re-prefill in the validator watchtower. A `LIVE` label elsewhere in H
  does not negate those gaps.
- Parameter anchors: [identity stake](https://flop.finance/intro/yellowpaper/#param-agent_identity_min_stake),
  [transaction count](https://flop.finance/intro/yellowpaper/#param-circuit_breaker_tx_count),
  [spend cap](https://flop.finance/intro/yellowpaper/#param-circuit_breaker_flop_cap),
  [window](https://flop.finance/intro/yellowpaper/#param-circuit_breaker_window).

## S2: Agent session overview

- URL: <https://flop.finance/intro/agent/>
- Class: `DRAFT_SPEC`; `SOURCE_VERIFIED`; page labels itself draft whitepaper.
- Read: five-field request summary, commitments/co-signed transcript, optional
  HARD quote, spending/delegation narrative.
- Decision: use as a design checklist only; detailed wire and implementation
  status require S1 and a separately selected supported SDK/runtime. Airdrop
  statements on this page are outside AgentForge implementation scope.

## S3: Miner role

- URL: <https://flop.finance/intro/miner/>
- Class: `DRAFT_SPEC` plus `IMPLEMENTATION_STATUS`; `SOURCE_VERIFIED`.
- Read: 10,000 FLOP base self-stake plus uncapped capacity-proportional exposure;
  rate under economic reconciliation; ordinary-GPU SOFT target, but end-to-end
  SOFT settlement/dispute lane still planned and current path attested.
- Decision: optional future infrastructure study only; no stake or GPU budget
  derived from this document and no miner behavior enabled.

## S4: Validator role

- URL: <https://flop.finance/intro/validator/>
- Class: `DRAFT_SPEC` plus `IMPLEMENTATION_STATUS`; `SOURCE_VERIFIED`.
- Read: block/proof commitments, DA model storage, governance and staking;
  ratified selection/cap versus partly wired implementation.
- Decision: distinguish chain validator duties from AgentForge task evaluation;
  do not infer hardware or independence from a role label. S1 records the
  target/current checker-lane distinction.

## S5: Verification design

- URL: <https://flop.finance/intro/verification/>
- Class: `DRAFT_SPEC` plus `IMPLEMENTATION_STATUS`; `SOURCE_VERIFIED`.
- Read: activation/transcript commitments, sampled turn/slice checks, disputes,
  evidence retention and challenge response; some mechanisms explicitly planned.
- Decision: execution evidence and task quality are separate. Do not encode
  generic “full-session re-run on every flag,” slash rates or audit percentages
  in AgentForge; detailed upstream rules and open items require version review.

## S6: FLOP status explainer

- URL: <https://ask.flop.finance/docs/what-is-flop>
- Displayed update date: **2026-08-19**.
- Class: `IMPLEMENTATION_STATUS`; `SOURCE_VERIFIED`.
- Read: explicit warning that the material describes a draft whitepaper and
  individual mechanisms are at different implementation stages.
- Decision: source claims, implementation claims and verified deployment
  evidence must be separate fields in future intelligence records.

## S7: TCLK specification

- Research commit: `5cc4ab93efbc8999a3a7e1471b639deca25998ea`.
- Pinned URL: <https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/SPEC.md>
- Class: `VERSIONED_PROTOCOL`; `SOURCE_VERIFIED`.
- Reviewed: introduction, §1, §2 transport/trust/retention bindings and start of
  §3 wire format; not a full protocol or cryptographic audit.
- Useful facts: convention/client layer distinct from settlement; public deal
  transcripts; signed records and room binding; unsigned venue time/sequence;
  untrusted notes; retention and room-epoch hazards.
- Decision: pin and use upstream after approval; never invent an interchangeable
  endpoint for the AgentForge event-envelope publisher or copy TCLK crypto.

## S8: TCLK changelog

- Pinned URL: <https://github.com/flop-labs/tclk/blob/5cc4ab93efbc8999a3a7e1471b639deca25998ea/CHANGELOG.md>
- Class: `IMPLEMENTATION_STATUS`; `SOURCE_VERIFIED`; complete changelog read.
- **Unreleased:** exact decimal-string nonces, schema-owned fields, rail registry
  and matching, non-authoritative heartbeat, complete signed-record folding,
  transcript/error handling, rail/receipt consistency and other hardening.
- **0.1.0, 2026-09-01:** alpha/testnet-only warning; no rail holding value in that
  release; unaudited reference adaptor cryptography; non-value PAPER rehearsal.
- Decision: do not describe Unreleased changes as guarantees of package 0.1.0.
  No hosted MCP access or package conformance was tested.

## S9: Unverified supplied social-post claims

Primary post URLs/dates/artifacts were not included in the supplied research.
The research summary is secondary context, not a primary source.

| Claim in supplied text | Class / verification | Decision |
|---|---|---|
| 13 million identities / 100 million messages | `MARKETING_STATEMENT`, `USER_SUPPLIED_UNVERIFIED` | Do not equate keys with independent operators or use counts in runtime logic |
| Probe experiment: statement/question/offer and 120-second response | `ECOSYSTEM_SIGNAL`, `USER_SUPPLIED_UNVERIFIED` | Internal telemetry hypothesis only, not a network SLA/reward rule |
| Coordinated voting disqualification warning | `ECOSYSTEM_SIGNAL`, `USER_SUPPLIED_UNVERIFIED` | Preserve honest ownership metadata regardless; do not claim a verified campaign rule |
| KOL referral tracking/conversion | `ECOSYSTEM_SIGNAL`, `USER_SUPPLIED_UNVERIFIED` | No referral analytics/farming module in AgentForge |
| GPU monitor as a planning feed | `ECOSYSTEM_SIGNAL`, not independently inspected | Optional future research; no price/capacity/freshness guarantee |

## Updating this ledger

Append dated changes to [CHANGELOG.md](CHANGELOG.md); keep previous observations
in version history. For every new source record its URL, immutable revision if
available, displayed date, retrieval date, reviewed scope, claim class, source
verification, upstream implementation status, and local evidence/decision.
Reject promotion from “source says” to “network verified” without executable
conformance and network-specific receipt evidence. Conflicts remain unresolved
until reviewed, rather than being silently overwritten with a convenient value.
