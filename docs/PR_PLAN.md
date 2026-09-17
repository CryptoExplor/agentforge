# Pull-request and commit plan

This plan keeps the GitHub history easy to review. The baseline import and the future provider work are separate.

## Baseline import commit

**Suggested subject:** `chore: import verified AgentForge MVP`

The baseline commit contains the current server, SDK, protocol contract, tests, Docker files, and the Markdown handoff/verification documents. It should not contain `.env`, `agentforge.db`, private keys, caches, or Python bytecode.

## Planned PR sequence

### PR 1 — Settlement provider boundary — **merged** (`4521722`)

**Suggested branch:** `refactor/settlement-provider-boundary`
**Suggested commits:**

1. `refactor: define settlement provider interface`
2. `refactor: route mock escrow through provider`
3. `test: preserve mock settlement invariants`
4. `docs: document provider boundary`

**Must preserve:** local mock behavior, Decimal accounting, event IDs, idempotency, slash destination, private balances, and terminal-state exclusivity.

**Must not add:** FLOP API names, imagined contracts, new client mode fields, or external provider behavior.

### PR 2 — Asset and mode guardrails — **merged** (`ecd9300`)

**Suggested branch:** `feat/server-derived-settlement-modes`

- Reject `FLOP` through the local provider.
- Keep `MOCK` and `TEST_CREDIT` local-only.
- Derive provider/deployment mode from server configuration.
- Keep eligibility statuses separate and server-derived.
- Add negative tests for client attempts to choose a network or eligibility status.
- Derive the primary escrow asset from the first funded component so zero-reward
  tasks funded in `TEST_CREDIT` are accepted.

### PR 3 — Durable external deal reference

**Suggested branch:** `feat/settlement-deal-reference`

Only add this when there is a concrete integration need. The model/migration may persist protocol, rail, contract/deal ID, offer/accept hashes, transcript digest, observed status, and terminal receipt. It must not persist secrets, keys, preimages, or private task payloads.

Migration acceptance requires:

- upgrade and downgrade coverage;
- no destructive data rewrite;
- authorization on deal reads;
- idempotent link/update behavior;
- explicit receipt verification status.

### PR 4 — Optional TCLK coordination adapter

**Suggested branch:** `feat/gated-tclk-adapter`

- Pin an official revision or checked-out MCP version.
- Keep the adapter thin, stateless where the upstream contract is stateless, and feature-flagged.
- Store hashes/references, not secrets or private task payloads.
- Treat PAPER/transcript outcomes as coordination observations, not external value settlement.
- Do not copy TCLK cryptography or duplicate its state machine.

### PR 5 — Official external provider

This PR is blocked until official testnet specifications/SDKs define the interface. It must include provider conformance fixtures, receipt verification, failure/retry behavior, and a public statement of exactly what is and is not verified. No airdrop logic belongs here unless a separate, explicitly approved product scope says so.

## PR review checklist

- [ ] Scope is one cohesive change.
- [ ] Existing tests pass.
- [ ] New behavior has focused regression tests.
- [ ] Idempotency and authorization were reviewed.
- [ ] Migrations have upgrade/downgrade coverage.
- [ ] `protocol/v1/openapi.json` is synchronized if routes/schemas changed.
- [ ] JSON Schemas validate.
- [ ] No secrets, database files, or generated caches are included.
- [ ] No FLOP/TCLK assumptions were invented.
- [ ] Docs identify deferred work and non-goals.
- [ ] `git diff --check` is clean.

## Commit hygiene

Prefer small commits that each leave the tree buildable. Avoid “fix everything” commits after the baseline. When a public protocol changes, put the implementation, tests, schema/OpenAPI update, and documentation in one cohesive PR, but keep unrelated refactors out.
