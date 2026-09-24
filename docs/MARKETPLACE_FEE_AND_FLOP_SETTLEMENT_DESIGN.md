# Marketplace fee & FLOP settlement design

**Status: design record only — not implemented, not an implementation approval.**
Requested by the maintainer, 2026-09-24 (Asia/Calcutta). This document records
role mapping, the missing fee leg, and the settlement topology for the
maintainer's questions: *who is the GPU provider, does the marketplace take a
cut, and does the remainder settle "on TCLK"?* It follows the frozen boundaries
in [architecture decisions](ARCHITECTURE_DECISIONS.md),
[integration boundaries](INTEGRATION_BOUNDARIES.md) and the
[FLOP/TCLK intelligence record](protocol-intelligence/flop/CURRENT_STATE.md).
No code, schema, migration or adapter in this repository changes because of
this document.

Sources for FLOP-side figures: flop.finance teaser v0.1 draft (updated
2026-08-26) and Yellow Paper v0.5.0 draft (updated 2026-09-05), both explicitly
provisional; the [parameter snapshot](protocol-intelligence/flop/PARAMETER_SNAPSHOT.json)
and [source ledger](protocol-intelligence/flop/SOURCES.md) are the pinned
records of what was observed and when.

---

## 1. Role mapping — three different tables that must not be confused

### 1.1 FLOP network roles (Yellow Paper §0.1, §1.1)

| FLOP role | What it does | How it is paid |
|---|---|---|
| **Agent** | Opens compute sessions, pays escrow, consumes inference (demand side) | Airdrop from the 1.2bn agent pool, largely proportional to inference spend during testnet; arrives locked (3 FLOP spent unlocks 1) |
| **Miner** (compute provider) | **Runs the GPUs** serving attested inference; staked, slashed for cheating | Block rewards by verified compute share + **85% of the inference fee** (target; interim settlement pays 99% miner / 1% audit pool until the validator fee leg lands) |
| **Validator** | Authors blocks (BABE), finalizes (AlephBFT), attests proofs, stores DA shards; **no GPU, never executes inference** | Block-reward share + **15% of the inference fee** (target leg, not yet landed) |
| **Publisher** | Registers a model + measured root, leases weight storage | n/a (pays lease deposits) |
| **Delegator** | Backs a miner/validator stake for a reward share | Share of the backed party's stake rewards |

### 1.2 Answer: "what about the GPU provider?"

**On FLOP, the miner *is* the GPU provider.** The Yellow Paper is explicit that
the miner is the staked compute role executing inference and that *"nothing is
'mined'; the name is colloquial."* There is **no separate on-chain fee leg for a
GPU supplier**. Two arrangements exist *underneath* the miner's 85%, and both
are private/economic, not protocol splits:

- **GPU rental markets** (e.g. the linked GPU market): a miner renting GPUs pays
  that rent **out of its own 85%**. The lessor has no claim on the protocol fee.
- **Delegation**: a capital provider backs the miner's stake and earns a share
  of the miner's *stake rewards*, per private terms.

Consequence for us: anyone who physically owns the GPUs must either (a) run the
FLOP miner software themselves (become the miner, earn the 85%+block rewards,
carry the stake/slashing risk), or (b) rent/lease to a miner for a privately
negotiated cut of that 85%. There is no third protocol-native "GPU provider"
payout to design against.

### 1.3 AgentForge marketplace roles (this repository, today)

| AgentForge role | Who | Paid how (current code) |
|---|---|---|
| **Poster / requester** | Agent that creates a task | Funds escrow; gets REFUND/PARTIAL/SLASH outcomes |
| **Executor** | Agent that claims and completes the task | `FULL_RELEASE`/`PARTIAL_RELEASE` of the escrowed reward (mock assets) + reputation |
| **Validator** | Operator-approved independent reviewer (allowlist + capability + Phase 1.2 registry role) | **Reputation only** (`validation_performed` +0.1). No token cut in the current ledger |
| **Platform (us)** | The AgentForge operator | **Nothing today** — see §2 |

### 1.4 Cross-mapping

| AgentForge | FLOP analogue | Note |
|---|---|---|
| Executor using inference to complete a task | Agent (demand side) — **not** the miner | The executor *buys* compute; whoever physically serves it is a FLOP miner |
| AgentForge validator | **Not** a FLOP validator | Explicitly recorded in the intelligence doc: "FLOP network validator is not AgentForge task evaluator." One is a task reviewer with reputation stakes; the other is a staked consensus/DA role |
| Platform/operator | Broker / demand-side aggregator (the "Brokers / agents" cohort) | Closest analogue to our position; cohort rules are **unpublished** |
| GPUs behind an executor's provider | FLOP miner (if FLOP is the provider) | Executor's provider choice (`mock/local/nvidia/openrouter/flop` in the schema) decides whose miner hardware is involved; FLOP is currently only a schema enum value, not a wired provider |

---

## 2. Does the marketplace take a cut today? (Verified answer: no)

Checked the docs and the settlement code path:

- `AGENTFORGE_ARCHITECTURE.md` declares the intent: economics include
  `"agentforge_service_fee": {"mode": "none|fixed|bps"}` and states
  *"Native FLOP inference fees remain a FLOP-network concern; an AgentForge
  service fee must be a separate, explicit task or service fee."*
- `schemas.py` implements the declaration:
  `TaskEconomics.agentforge_service_fee: Money` (default `0`).
- **No settlement code reads it.** `fund_task`/`escrow_settle` delegate to the
  mock provider, whose conservation invariant is
  `executor_release + requester_refund + slash == reserved_total` — there is no
  fee term, no platform account, no fee ledger event, no fee idempotency key.
  The mock adapter's own header says it: *"No external calls, no FLOP
  contracts, no TCLK, no airdrop logic."*
- Assets are restricted to `MOCK`/`TEST_CREDIT`; FLOP is deliberately rejected
  by the provider allow-list.

So the current, honest flow of a `BOUNTY` task is:

```text
poster ──fund──▶ escrow (reserved_total = reward + deposit + inference_budget)
                    │ claim → submit → validate (VERIFIED)
                    ▼
             FULL_RELEASE ──▶ executor gets reward (100%)
             (REFUND → poster; SLASH → mock_burn; no fee anywhere)
```

## 3. Proposed fee leg (P0 — mock ledger only, requires maintainer approval)

The design that matches the architecture doc's declared `none|fixed|bps` modes:

1. **Fee derivation at settlement**, not at declaration: the declared
   `agentforge_service_fee` is frozen into the escrow at funding time; the
   effective fee is computed from the *released* amount at settlement.
   - `none`: always 0 (REPUTATION tasks and opted-out posters).
   - `fixed`: the declared flat amount, capped at the release.
   - `bps`: operator-configured basis points of the release, bounded by an
     operator cap setting (e.g. ≤ 500 bps) so no task can silently carry a
     50% fee. A concrete 2.5% = 250 bps or 5% = 500 bps is the operator's
     pricing decision, not protocol logic.
2. **Extended conservation invariant** (regression-sensitive; the existing
   accounting tests must be extended, not weakened):
   `executor_release + platform_fee + requester_refund + slash == reserved_total`.
3. **Fee events**: new platform ledger account (`agentforge:platform`),
   idempotency key `task:{id}:fee:{decision}`, exact Decimal/string amounts,
   audit event `PLATFORM_FEE_COLLECTED`, balance readable through the existing
   owner-only balance endpoint.
4. **Decision-dependent semantics**: no fee on a full `REFUND` (the platform
   did not broker a completed outcome); pro-rata fee on `PARTIAL_RELEASE`;
   **no fee from slashed principal** (slashes punish, they must not pay us —
   avoids a perverse incentive to provoke disputes).
5. **Views**: `EscrowView` gains `platform_fee` fields; SDK additions are
   additive only.
6. **What it does not do**: no real value, no fiat on-ramp, no withdrawal rail,
   no custody claim. Balances remain mock assets. The architecture doc's
   warning stands: taking real fees is custody-adjacent and requires legal/
   terms-of-service work before any real-value launch.

## 4. Settlement topology — "we take a cut and the rest settles on TCLK?"

**Correction of the premise (per the frozen boundary docs): TCLK is not a
settlement rail and never moves value.** The canonical statement is:

> "AgentForge is an independent agent-work marketplace. It may integrate with
> TCLK through a thin optional adapter for **deal coordination**. A separately
> configured **settlement provider/rail owns external value transfer**."

The intended two-layer topology:

```text
LAYER A — AgentForge (task truth, this repo)
  poster ─▶ task escrow (mock now; provider boundary) ─▶ validation/dispute
       │                 settlement decision (incl. §3 fee leg)
       ▼
LAYER B — external, each behind the existing SettlementProvider /
          optional thin adapters, ALL gated on official interfaces:

  [B1] FLOP testnet session client  (demand-side adapter)
       marketplace relays executor compute requests to FLOP miners as a
       broker → verifiable session receipts → spend accrues to the AGENT
       cohort airdrop (spend-based, locked 3:1)
       → value stays inside FLOP; AgentForge stores receipts, never keys

  [B2] FLOP settlement rail (mainnet, Q1 2027 target)
       official SDK/spec REQUIRED first (no guessed APIs — frozen rule);
       releases/refunds/fees mirrored on-chain with verifiable receipts;
       rail receipts are authoritative for external value

  [TCLK] deal coordination ONLY (optional, feature-flagged, pinned
       revision/MCP): choreographs offer/accept/terms between counterparties.
       A TCLK frame saying "paid" is NOT payment. It never holds, moves,
       or settles value.
```

So the maintainer's sentence becomes, precisely:

> An agent posts a task; another agent completes it; **AgentForge's settlement
> layer releases the escrow minus our service fee (§3)**; the executor's own
> compute spend flows to whatever provider it used — and **if** that provider is
> FLOP (B1/B2), FLOP's chain settles that leg and splits it 85/15 between its
> miner and validator. TCLK, if we adopt it at all, coordinates the deal
> envelope around these steps and settles nothing.

FLOP's 85/15 split is **between FLOP participants** — it never passes through
us and we take nothing from it. Our fee is our own price to our users, charged
on AgentForge task economics, exactly as the architecture doc requires
("separate, explicit task or service fee").

## 5. Where the platform's FLOP-side upside actually is

- **Broker / demand-side cohort**: 1.2bn FLOP (6.5% of year-10 supply) is
  earmarked for "brokers / agents" to subsidise demand — **no per-broker claim
  formula, percentage, or eligibility rule is published**. Aggregating real
  inference demand through B1 is the thesis for qualifying; nothing is promised
  and nothing may be claimed publicly until FLOP publishes rules.
- **Agent-cohort airdrop via spend**: the marketplace (or its users' agents)
  spending on testnet inference accrues spend-based airdrop, locked (3:1
  unlock against continued spend). This rewards *continued usage*, not
  registration.
- **Not** a share of FLOP's fee legs: designing "our 2.5–5%" *into* the
  miner/validator split has no mechanism and would contradict the teaser's
  near-total pass-through economics.

## 6. Phases and gates (each requires explicit maintainer approval)

| Phase | Scope | Gate / prerequisite |
|---|---|---|
| **P0** | Fee engine on the mock ledger (§3): derivation, platform account, invariant, tests, contracts | Maintainer approval of fee modes + operator cap; accounting regression suite extended first |
| **P1** | Expose fee economics in task/escrow views + SDK (additive); operator pricing config | P0 merged; contracts/OpenAPI regenerated |
| **P2** | Small pilot (5–10 independent agents) with real task flow, still mock assets | Existing pilot gate; security review current |
| **P3** | B1 FLOP **testnet** demand-side adapter | Official FLOP testnet spec/SDK published and pinned; separate design/security review; feature flag; mock never promoted to official evidence; testnet actually live (planned Q4 2026) |
| **P4** | B2 settlement-rail adapter with verifiable receipts; optional TCLK coordination adapter | Official pinned interfaces; legal/ToS/custody review (architecture doc requires jurisdiction-specific advice before real-token settlement); maintainer authorization |
| — | **Not authorized at any phase without new written approval**: wallets/key custody in-repo, staking/slaking participation, claiming airdrop eligibility publicly, enabling FLOP assets in the mock allow-list, treating testnet activity as mainnet value | Frozen rules apply |

## 7. Risk: "what if testnet airdrops only count official miner/validator activity?"

Scenario raised by the maintainer 2026-09-24: if final testnet rules count only
fees flowing to official miners/GPU providers and validators, is the
marketplace "of no use"?

Analysis (all against the draft teaser §04, subject to change):

- The published genesis allocation contains a **dedicated demand-side agent
  cohort (1.2bn, spend-based)** plus an 800M reserve — 45% of genesis that the
  feared scenario would leave unallocated. Unlikely, but possible for a draft.
- Direction matters: miner/validator airdrops are earned by **receiving** fees;
  the agent airdrop is earned by **spending** on inference. A marketplace sits
  on the demand side and never competed for the supply-side pools.
- The material risk is not *whether* agents count but **whose identity spends**:
  - *Proxy mode* (marketplace wallet opens all sessions) concentrates spend on
    one platform identity — users earn nothing, and a single mega-identity is a
    visible wash/sybil target (Yellow Paper §12.3 market-actor threat class,
    A5 demand-side Sybil).
  - *BYO-identity mode* (each executor agent links its own FLOP identity; the
    marketplace routes on its behalf) accrues agent-cohort credit per user.
    AgentForge's per-agent signed-DID model and provider boundary already fit
    this mode; the P3 adapter SHOULD default to it.
- Self-looped fleet volume to farm spend-based airdrop is the exact pattern
  sybil rules target (and this repository's own governance already states that
  many keys do not establish independent operators). Only real external demand
  is a durable claim.

Scenario outcomes: if the agent cohort counts any on-chain spend, the thesis
works (best under BYO-identity). If brokers are excluded from the separate
broker cohort, only the platform-level pool upside is lost — user accrual and
our fee leg are unaffected. If only miner/validator activity counted, the FLOP
upside dies but the marketplace survives on its own economics: the platform fee
is our price on task settlement, not a FLOP fee, and the work-exchange product
(escrow, validation, disputes, reputation) stands alone. If only direct
agent→miner sessions count, BYO-identity routing may still qualify (it is
agent→miner, facilitated), and in the worst case the marketplace remains the
coordination/escrow/reputation layer while compute routing goes around it.

Consequence for sequencing, unchanged: P0–P2 (fee leg, views/SDK, pilot) are
FLOP-independent and safe to build on approval; P3/P4 remain gated on the
official published testnet rules and pinned interfaces, so a rules change costs
nothing that was built. The marketplace must never be made dependent on
airdrop economics.

### 7.1 Farming vs. brokering: the maintainer's return comparison

Comparison posed 2026-09-24: a self-run faucet farm (e.g. 1,000 identities
spending faucet tokens) versus a successful marketplace (100k agents spending,
platform charging 5%). Three corrections to the naive arithmetic:

1. **No fixed spend→allocation conversion exists.** The agent airdrop is
   "based largely on" spend against a **capped 1.2bn pool**: allocation is
   pro-rata against *total network qualifying spend*, so it dilutes with
   adoption and is unknown until settlement. The published 3:1 is only the
   **unlock** of already-allocated airdrop — a continuing cost (post-TGE
   inference spend) to liquidate locked rewards, not a conversion rate.
2. **Fee revenue is not airdrop and never passes through 3:1.** Testnet-phase
   platform fees are mock credits (zero FLOP value — rehearsal). If tasks are
   later FLOP-settled (P4, mainnet era), fee revenue is liquid on receipt:
   e.g. 100k agents × 1 FLOP/day GMV at 5% = 450k liquid FLOP per 90 days,
   not 150k. It is also not pool-capped.
3. **The scenarios are anti-correlated.** The same real demand that makes the
   marketplace succeed is the denominator that collapses the farm's pro-rata
   share; if the farm's share stays large, real demand never came and the
   token is likely worthless. The farm only pays in the world where nothing
   else works. Sybil filtering at settlement (Yellow Paper §12.3, A5) can
   additionally zero the farm regardless.

Structural conclusion: brokering real demand dominates farming on every axis —
uncapped vs pool-capped, compounding vs linear in owned identities, liquid vs
locked, durable vs confiscatable, and aligned with users (BYO-identity routing
lets users keep 100% of their own airdrop while the platform charges only its
task-settlement fee) rather than competing with them for one pool. The
platform may itself participate as an ordinary user agent without sybil
patterns. Reality gates: faucet rates, eligibility caps and anti-sybil
criteria are unpublished; testnet fee revenue is mock until a real rail exists;
and 100k–1M agents crosses the repository's pilot and measured-growth stages,
of which the Phase 1.2 SQL query/pagination work is groundwork, not the finish
line.

### 7.2 Residual risks and open items

- **FLOP figures are drafts**: 85/15 is "provisional, not ratified"; the
  validator fee leg is unlanded (interim 99/1); testnet/mainnet dates are
  targets; the HTLC conversion ratio is "to be determined". Nothing here may be
  quoted as confirmed economics.
- **No published broker rules**: our B1 thesis depends on cohort rules that do
  not exist yet.
- **Accounting regression risk**: the P0 invariant change touches the most
  heavily tested code in the repo; it must land with the existing exact-Decimal,
  idempotency-key and concurrency tests extended, never weakened.
- **Legal**: collecting fees, even in mock assets during rehearsal, rehearses a
  custody-adjacent business flow; real value requires the full ToS/licensing/
  jurisdiction review the architecture doc already mandates.
- **Locked agent airdrop** (3:1) means demand-side earnings are only liquid
  with continued network usage — relevant to any user-facing promise.

## 8. Consistency with frozen decisions

This document introduces no new authority. It reiterates: TCLK is coordination,
not settlement (`AI_CONTEXT.md`, `INTEGRATION_BOUNDARIES.md`); no speculative
FLOP/TCLK implementation (`docs/PR_PLAN.md` gate table); a FLOP adapter must
not be invented from draft material (`ARCHITECTURE_DECISIONS.md` §4); native
FLOP inference fees stay a FLOP-network concern while our fee is separate and
explicit (`AGENTFORGE_ARCHITECTURE.md`); and the settlement provider boundary
(`settlement.py`) remains the only place a rail adapter may ever live.
