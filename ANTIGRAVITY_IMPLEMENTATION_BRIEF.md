# AgentForge — Antigravity Implementation Brief

Read `AGENTFORGE_ARCHITECTURE.md` first. Implement the pre-testnet MVP described there. Do not invent FLOP-specific contracts, airdrop logic, or settlement rules.

## Product boundary

AgentForge is an open work and service orchestration layer. It coordinates agents, tasks, inference requests, proofs, validation, reputation, and mock escrow. Technocore is an optional coordination/evidence adapter. FLOP is a future inference and settlement provider.

## Required implementation choices

- FastAPI service with PostgreSQL as authoritative state.
- Redis optional; never use it as the source of truth.
- Versioned API under `/api/v1`.
- Ed25519 `did:key` challenge/response registration.
- Signed state-changing requests with timestamp, nonce, body hash, and idempotency key.
- Canonical JSON for hashes and signatures.
- Append-only audit/domain events.
- Mock inference and mock escrow labelled `MOCK`; never represent them as FLOP usage.
- Technocore publication through an asynchronous retryable outbox.
- External worker code must never run inside the API process.

## Core execution flow

```text
real demand
  -> Task + demand provenance
  -> ExecutionPlan
  -> InferenceRequest(s)
  -> InferenceProvider
  -> InferenceReceipt(s)
  -> TaskResult + ProofBundle
  -> deterministic/domain validation
  -> signed ValidationDecision
  -> reputation + mock escrow settlement
```

## InferenceProvider interface

Use this generic interface. Do not add FLOP-specific challenge operations to it:

```python
class InferenceProvider(Protocol):
    async def quote(self, request: InferenceRequest) -> Quote: ...
    async def create(self, request: InferenceRequest) -> InferenceSession: ...
    async def status(self, session_id: str) -> InferenceStatus: ...
    async def result(self, session_id: str) -> InferenceResult: ...
    async def receipt(self, session_id: str) -> InferenceReceipt: ...
    async def cancel(self, session_id: str) -> None: ...
```

Support `Mock` first. Keep provider-specific operations inside provider implementations.

Every receipt must distinguish:

```text
requested_compute
measured_compute
paid_compute
verified_compute
```

## Non-negotiable policies

1. No real demand means no reputation-bearing or paid task.
2. Synthetic demos are `DEMO_ONLY`.
3. Self-reported provenance is not sufficient for high-value eligibility.
4. Acceptance criteria are frozen after claim.
5. Requester cannot veto valid work after submission; it must dispute.
6. Escrow transitions are idempotent and mutually exclusive.
7. Partial settlement is supported.
8. A retry must never pay twice or create duplicate reputation events.
9. AgentForge service fees are separate from native FLOP inference fees.
10. Mock/external inference is never an official FLOP participation receipt.

## Required models

Implement at minimum:

```text
Agent
AgentKey / nonce state
Capability
Task
DemandProvenance
Claim
ExecutionPlan
InferenceSession
InferenceReceipt
Submission
ProofBundle
ValidationDecision
Dispute
Escrow
SettlementReceipt
ReputationEvent
ActivityEligibility
AuditEvent
OutboxEvent
```

Reputation must be separate for executor, requester, and validator roles, and capability-specific.

Track operator/infrastructure groups privately or access-controlled for validator independence and anti-collusion checks. Derive anti-circularity from server history; never trust a client-submitted `blocked_pairs` result.

## Lifecycle

```text
OPEN
 -> FUNDED
 -> CLAIMED
 -> EXECUTING
 -> SUBMITTED
 -> VALIDATING
 -> VERIFIED | REJECTED | PARTIAL | DISPUTED
 -> SETTLEMENT_PENDING
 -> SETTLED
```

Also support `EXPIRED` and `CANCELLED` with explicit rules. Disputed escrow is frozen. `RELEASED`, `REFUNDED`, and `SLASHED` are terminal and mutually exclusive.

## Validation

- Deterministic checks first.
- Domain validators only when required.
- Signed immutable validation decisions.
- Randomized independent dispute panel for disagreement/challenge.
- Exclude executor, requester, same operator/group, recent collaborators, and excessive reciprocal validators.

## Security requirements

- Do not print or expose private seeds.
- Validate that loaded private keys derive their stored DID.
- Use secure file permissions for local identity material.
- Protect against replay, SSRF, oversized payloads, prompt injection, arbitrary code execution, and task/API spam.
- Add task runtime, memory, storage, network, RPC, and LLM budgets.
- Redact credentials and sensitive task data from logs.

## Delivery order

### Phase 1

Schemas, migrations, DID verification, canonical signing, domain state machines, and unit tests.

### Phase 2

Task API, filters, PostgreSQL claims, leases, heartbeats, mock inference, mock escrow, and Python SDK.

### Phase 3

Proof bundles, deterministic validators, signed validation decisions, reputation, activity eligibility, demand provenance, and anti-circularity.

### Phase 4

Technocore outbox, conformance fixtures, failure tests, simple UI, and a 5–10-agent pilot.

## Explicit non-goals

Do not implement yet:

- real FLOP token settlement;
- FLOP airdrop scoring or eligibility claims;
- custom GPU marketplace;
- federation;
- subscriptions;
- complex bidding;
- unrestricted execution hosting;
- automatic task generation without provenance.

## Definition of done

An independent developer can generate a DID, register a capability, discover a task, claim it without a race, execute through a provider, submit a signed result and proof, receive a validation decision, observe reputation and mock escrow settlement, and inspect a retryable Technocore publication.

## Frozen audit-fix boundary

The standalone pre-testnet implementation now fails closed on expired claim leases
and late submissions, persists idempotency records for every signed mutation,
and uses the database partial unique index as the active-claim race backstop.
Outbox workers lease rows before delivery and adapters must deduplicate event IDs.

Client provenance fields (`source_ref`, `novelty_hash`, `attestation`, and
`level`) are evidence only. A server-registered source adapter must verify the
complete claim before the trusted level can exceed zero. The MVP has no default
network source adapter.

Private task-linked reads, balances, and audit events are authenticated. Audit
pagination uses an opaque cursor ordered by event timestamp and ID. Deterministic
validation checks hashes, proof identity, acceptance required outputs/evidence,
optional declarative JSON Schema criteria, inference receipt ownership/integrity,
and deadlines independently of validator-supplied checks.

The mock ledger exposes mutually exclusive `FULL_RELEASE`, `PARTIAL_RELEASE`,
`REFUND`, and `SLASH` transitions. A requester-subject slash uses the explicit
`mock_burn` destination and credits no account. Production uses PostgreSQL and
explicit Alembic migrations; it does not auto-create SQLite schemas. Development
faucet/auto-schema settings are isolated in the dev Docker files.
