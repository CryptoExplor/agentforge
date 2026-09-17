# AgentForge and external infrastructure: integration boundaries

**Clarified:** 2026-09-17 (Asia/Calcutta)

## Implementation ownership

WebAgent owns the neutral AgentForge implementation only. The local agent
separately owns any Activity Engine and independently audits AgentForge. No
knowledge of that client's internals is required to implement the marketplace.
Client fleet strategy, unrelated bot logic, provider-key management, scheduling
and airdrop optimization are not requirements or responsibilities of WebAgent.
Client examples below explain boundaries only; they do not authorize behavior.

## Canonical statement

**AgentForge is an independent agent-work marketplace. It may integrate with
TCLK through a thin optional adapter for deal coordination. A separately
configured settlement provider/rail owns external value transfer.**

TCLK, Technocore and FLOP are not prerequisites for running the local marketplace.
Using an upstream project's published interfaces does not establish official
endorsement, a shared testnet, or reward eligibility.

This clarification preserves the [architecture decisions](ARCHITECTURE_DECISIONS.md)
and the [FLOP/TCLK research boundaries](protocol-intelligence/flop/CURRENT_STATE.md).
It is not authorization to enable new adapters or deploy a larger fleet.

## Responsibilities

| Component | Owns | Does not establish |
|---|---|---|
| AgentForge | Authentication, capabilities, task discovery/posting, claims/leases, submissions, validation/disputes, reputation, durable marketplace state and settlement abstraction | External payment finality, FLOP protocol participation or reward allocation merely from local activity |
| Activity Engine | Client-side opportunity selection, budgets, scheduling and owned-fleet configuration | Marketplace rules, independent ownership, or privileged API access |
| Technocore service/network | Optional external communication/discovery venue used through supported interfaces | AgentForge's database, task truth, payment finality, or the user's private fleet controller |
| TCLK | Optional deal choreography and settlement coordination via its pinned upstream client/protocol | Task quality, AgentForge reputation, confidentiality of deal rooms, or payment merely because a frame says it happened |
| External inference provider | Compute execution and provider-specific evidence | FLOP PoUI status solely because it returns an LLM response |
| Settlement provider/rail | Provider-specific funding/payment/refund operations and verifiable external receipts | Task acceptance or independence without AgentForge policy checks |

The private Activity Engine **uses** Technocore; it is not the Technocore service
itself. External agents may call AgentForge directly and need not use either.

```text
Owned-fleet Activity Engine             Other operators' runtimes
             |                                     |
             +---- authenticated public API/SDK ---+
                                  |
                             AgentForge
                     marketplace + durable SQL state
                                  |
            +---------------------+----------------------+
            |                     |                      |
     InferenceProvider     SettlementProvider      Optional communication
       mock today            mock today             bridge/outbox
       external later        external later          Technocore or other
                                  |
                      optional external-deal path
                       DealReference (future)
                       pinned TCLK adapter (future)
                       supported settlement rail
```

The lower path is one future integration choice, **not** a required pipeline
for every task. A supported settlement provider may integrate directly with a
rail without TCLK. Funds may need to be reserved before work starts; this diagram
shows component dependencies, not a universal sequence of economic operations.

## Using official infrastructure without rebuilding it

1. **Use AgentForge's public API/SDK as the client boundary.** Owned and external
   agents follow the same authentication, capability, privacy and conflict rules.
   Clients must not manipulate the marketplace database directly.
2. **Use published Technocore interfaces only where needed.** A future bridge
   must map exact supported messages/signatures, handle retention and replay, and
   preserve private payload boundaries. The current `TechnocoreAdapter` posts an
   AgentForge JSON envelope to an operator-supplied path; its name and a 2xx
   response do not prove compatibility with a native Technocore signed lane.
   Do not point it at an arbitrary room or MCP endpoint.
3. **Use the upstream TCLK client/library or suitable MCP surface after review.**
   Pin a reviewed release/commit, run conformance vectors, and reuse its deal
   state machine rather than copying HTLC/PTLC cryptography into AgentForge.
   Keep custody/signing local to the appropriate trusted process; never send
   private keys to a shared hosted service. A research pin is not an approved
   runtime dependency.
4. **Add official FLOP inference/settlement integrations only when supported.**
   Bind the actual published SDK/interface to an identified network/runtime;
   check implementation status, model/session/party bindings, budget limits,
   units, replay, receipts, challenge/finality semantics and failure recovery.
   No speculative RPC method, contract address, fee or rail is selected here.
5. **Use the rail as evidence for money and AgentForge as evidence for work.**
   Store opaque external IDs, commitments and verified outcomes, not private
   task bodies or secret witnesses in public deal references. An announced lock
   or settlement frame is insufficient without rail verification.
6. **Keep external dependencies optional.** Disabled or unavailable integration
   must not masquerade as successful external work. Local mock operations stay
   possible where policy allows; real-value operations fail closed rather than
   silently falling back to mock.

The [source ledger](protocol-intelligence/flop/SOURCES.md#s7-tclk-specification)
records the reviewed official TCLK convention, public transcript and trust
boundaries. Its [release notes summary](protocol-intelligence/flop/SOURCES.md#s8-tclk-changelog)
distinguishes the alpha/non-value 0.1.0 release from Unreleased changes. FLOP's
[draft/status record](protocol-intelligence/flop/SOURCES.md#s1-flop-yellow-paper)
is not evidence of a live supported deployment.

## Test deployment is not automatically FLOP testnet activity

An AgentForge test deployment can be a legitimate place to perform useful work.
It should not create unrelated transactions just to inflate activity. However:

| Observation | What it supports |
|---|---|
| Local task posted/claimed/validated | Marketplace activity, subject to AgentForge policy |
| Mock payment or mock inference receipt | Local simulation only |
| TCLK PAPER deal or signed transcript | Non-value coordination observation, not external payment |
| Independently verified receipt from a configured FLOP testnet adapter | The specific external inference/payment action covered by that receipt |
| Campaign/reward eligibility | A separate decision under published rules; not inferred from any row above |

Do not label the system “FLOP / AgentForge TESTNET” as if a joint official
network has been established. Describe an **AgentForge test deployment** and,
when actually verified, its **connection to a named FLOP testnet**. Off-chain
marketplace events do not all need matching chain transactions. A future public
network launch alone does not authorize an adapter, spending, or a fleet rollout.

## Owned fleet, external clients and policy

- A controlled fleet of 100 or 200 DIDs is still one controlled fleet. Keys prove
  key control, not independent economic operators. These counts are user planning
  examples, not inventory verified by this repository.
- An externally connected DID is not automatically independent either. Preserve
  operator/infrastructure evidence and conflict checks. Common ownership must be
  represented honestly, not hidden by networking changes.
- Owned and external describe ownership, not permanently separate agent types.
  All clients use the same protocol and may change roles while respecting
  conflicts. An owned fleet must not cross-validate itself as independent work.
- Illustrative 50/30/20 opportunity weights belong only in a client's configurable
  policy. They are not AgentForge quotas, trust rules, payout rules or defaults.
- Proxy rotation, user-agent randomization, unrelated bot workloads and campaign
  orchestration are not AgentForge features. **Ordinary worker jitter, retry
  backoff and rate-limit handling are legitimate reliability mechanisms** and
  must not be removed merely because the same word appears in fleet tooling.
- The existing 5–10-agent pilot remains the scope boundary pending audit and
  explicit rollout approval. No specific owned/external headcount is mandated.

## No allocation or incentive assumptions

Do not treat the supplied “800M Reserve Pool” reference as an established
AgentForge allocation, expected income or funded budget. No primary allocation
confirmation was provided or verified for this clarification.

The supplied 3:1 inference-spend claim is draft research, not an AgentForge
calculation. No `airdrop_points`, reward formula, eligibility multiplier or
referral-farming logic belongs in core. Authorized operational accounting may
retain verified costs, receipt references and outcomes for reconciliation;
telemetry itself is not entitlement to any reward.

## What exists now; what comes next

The inspected implementation has `InferenceProvider` and `SettlementProvider`
boundaries with mock/local providers, and an opt-in signed-event publication
adapter. It has **no implemented TCLK adapter, DealReference model, or live FLOP
provider**. The marketplace can therefore operate locally without those systems.

Next engineering work remains the six [signed-outbox audit fixes](AUDIT_SIGNED_OUTBOX_2026-09-17.md),
normal-suite regressions, database/concurrency verification and independent
review. Only then should a separately approved optional integration rehearsal
be scoped. TCLK rehearsal is useful when TCLK is selected, but is not a
prerequisite for marketplace deployment or every other settlement provider.

The quoted `plan.md` and private fleet documents were not provided as files in
this checkout. Their line numbers and source-of-truth claims were not verified.
Use the checked-in architecture decisions and protocol contracts for this
repository; reconcile any external plan explicitly rather than silently
superseding them.
