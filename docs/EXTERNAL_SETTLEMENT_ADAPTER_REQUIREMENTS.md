# External settlement adapter intake requirements

**Status: design/security gate; no external settlement provider is enabled.**

AgentForge currently accepts only `AGENTFORGE_SETTLEMENT_PROVIDER=mock|local`.
The mock ledger is transactional database code. It is not a testnet wallet and
must not be presented as one. This checklist deliberately precedes any chain
adapter: `SettlementProvider.fund()` and `.settle()` are currently called inside
the marketplace database transaction, so putting RPC calls in those methods
would hold SQL locks across unbounded network I/O and create dual-write failure
modes.

## Evidence required before implementation

1. **Official interface:** canonical testnet documentation, chain ID/genesis,
   RPC transport, finality model, faucet, contract/pallet addresses and ABI or
   metadata. Record source URLs and retrieval dates.
2. **Pinned artifacts:** exact SDK and interface versions/digests; verify release
   signatures or Cosign identity and issuer where supplied. Do not follow an
   unpinned branch at runtime.
3. **Parity statement:** enumerate differences between devnet, simulator and
   testnet (assets, finality, reorgs, fees, nonce rules, receipts and reset
   policy). A simulator result is never testnet evidence.
4. **Asset semantics:** decimals, minimums, fee payer, escrow authority, token
   identity and whether the asset is transferable or has real value.
5. **Threat model:** compromised RPC, equivocation, reorg, delayed finality,
   dropped/replaced transactions, nonce races, malicious receipts, key theft,
   chain halt and contract upgrade/admin-key risk.

## Required architecture

A live rail needs a separate durable state machine, not synchronous hooks in the
request transaction:

- Commit marketplace intent and a uniquely keyed settlement attempt in one SQL
  transaction/outbox operation.
- Let a worker perform RPC submission after that transaction commits. Never
  hold database locks or sessions while waiting on DNS, RPC, signing hardware,
  mempools or finality.
- Persist attempt number, idempotency key, canonical unsigned intent, submitted
  transaction hash, observed block/hash, finality depth and sanitized errors.
- Reconcile independently. Verify chain ID, contract/pallet identity, event
  signature/topics, asset, amount, parties and unique intent ID from the receipt;
  a provider's `success=true` is insufficient.
- Model at least `PENDING`, `SUBMITTED`, `CONFIRMED`, `FINALIZED`, `FAILED`,
  `REPLACED`, `REORGED` and `MANUAL_REVIEW`. Ambiguous outcomes must not be
  retried as new economic actions.
- Make `hold`, `release`, `refund` and `slash` idempotent by marketplace intent,
  with terminal-transition exclusivity and conservation checks equivalent to
  the mock ledger. Define partial execution and fee accounting explicitly.
- Separate transaction submission from receipt verification. Use different
  ports/types so tests cannot satisfy verification with a submitter's claim.
- Keep signing keys in a secret manager/HSM-backed signer, never environment
  examples, logs, database payloads or the repository. Specify rotation,
  withdrawal limits and emergency pause.

## Review and acceptance gate

Before enabling a provider name, require:

- an architecture decision and migration/rollback plan;
- security review of pinned interfaces and receipt fixtures;
- replay, duplicate, crash-between-steps, timeout, replacement, reorg and
  reconciliation tests;
- testnet evidence with transaction and finalized receipt identifiers;
- operational alerts for age/backlog, uncertain attempts, balance drift and RPC
  health;
- a documented failure/manual-recovery policy that never edits the append-only
  audit trail; and
- an explicit policy change to `get_settlement_provider()` plus deployment
  authorization.

Until those gates pass, staging remains `mock`, gossip remains disabled and no
`AGENTFORGE_RPC_URL` or contract-address setting is accepted. This is intentional
fail-closed behavior, not missing configuration.
