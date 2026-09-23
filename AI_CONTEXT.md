# AgentForge: coding-agent entry point

Read this file, then the two canonical records below. Do not copy their status,
test counts or historical timelines into another handoff document.

1. [Current state and GitHub snapshot](docs/PROJECT_STATUS.md)
2. [Audit evidence, reproduction and remaining limits](docs/AUDIT_VERIFICATION.md)
3. For implementation work: [security controls](docs/SECURITY_REMEDIATION.md),
   [accounting controls](docs/ACCOUNTING_REMEDIATION.md),
   [outbox contract](docs/EVENT_OUTBOX.md), [repository map](docs/REPOSITORY_MAP.md).

## Standing workflow

- Work only on the session-assigned branch. This session is
  `arena/01a0af8f-agentforge`. Preserve existing working-tree changes.
- Web agent implements; a separate local/auditing agent reviews and tests;
  **human maintainer alone decides and performs merges**.
- Never merge/close PRs, self-approve, push `main`, force-push or rewrite shared
  history. Report any unexpected merge; the maintainer decides whether to revert.
- Green tests, CI or mergeability are not approval. Verify live GitHub state
  before making publication claims; local and remote heads may differ.
- No credentials in chat, repository, proofs or logs. Use existing GitHub
  authentication; a broken connection must be reconnected through Arena.

## Scope boundaries

AgentForge is a neutral marketplace. The separate Activity Engine is an ordinary
API/SDK client owned by the local agent, not part of marketplace implementation.
No fleet strategy, provider-key management, scheduling, OpenSea or airdrop logic
belongs in the core. Its requested source is absent from this checkout; consult
[the external-client review](docs/ACTIVITY_ENGINE_P0_REVIEW.md), not imagined files.

Preserve public signing/contracts and flat Python SDK compatibility. Do not
start SDK modularization, broker/MCP/discovery runtime or deployment work before
its security and approval gates. The accepted designs remain in
[SDK plan](docs/SDK_ARCHITECTURE_PLAN.md) and
[discovery plan](docs/DISCOVERY_SCALABILITY_PLAN.md); they are not shipped features.
TCLK is coordination, not settlement. Do not invent FLOP interfaces from drafts.
`scripts/flop-agent-starter/`, if present, remains quarantined: do not modify or
run it before an officially authorized testnet phase.

## Maintaining these docs

- `PROJECT_STATUS.md`: current state, blockers and GitHub snapshot only.
- `AUDIT_VERIFICATION.md`: commands, measured results and audit scope only.
- Remediation/architecture documents: technical policies and compatibility.
- Dated audit/readiness documents: historical evidence, explicitly labeled.
- `GITHUB_HANDOFF.md` and `PR_PLAN.md`: workflow links and gates, not repeated context.
