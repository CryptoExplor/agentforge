# AgentForge project status

**Canonical current-status summary — 2026-09-18 (Asia/Calcutta).**

AgentForge is a **pre-testnet, neutral agent-work marketplace**. It is not a
public-ready service, an official FLOP client, a fleet controller or a real-value
settlement rail. Implementation-side tests are not independent approval.

## Current work

Implemented in this review revision:

- Signed-outbox audit fixes F1–F6: atomic expiry, complete v2 causation, publisher
  preflight, versioned schema enforcement, startup guards and fresh retry state.
- D1–D6 security controls: operator-approved validators, bounded acceptance
  schemas/ingress, SQL-shared admission, safe route ordering and decimal parsing.
- Exact transactional mock accounting, replay-content checks, concurrent account
  creation and guarded settlement/validation/dispute/cancel/claim transitions.
- Settlement configuration checked before cached/injected provider resolution;
  unsupported configuration cannot silently reuse the mock singleton.
- Bounded mock-inference cache; configuration URL redaction in worker status logs.
- Atomic, private-from-creation SDK identity saves; failures preserve the old
  file and target symlinks are not followed.
- Server and standalone SDK dependency floor `cryptography>=50.0.1,<51`, following
  advisory scanning. Existing SDK methods and signing formats are unchanged.
- Current status, verification, technical policy and roadmap docs separated to
  remove repeated handoff prompts and contradictory historical test counts.

See [verification and audit findings](AUDIT_VERIFICATION.md) for exact test counts,
commands, dependency evidence and limitations. Technical controls live in
[security remediation](SECURITY_REMEDIATION.md),
[accounting remediation](ACCOUNTING_REMEDIATION.md) and [event outbox](EVENT_OUTBOX.md).

## Review publication

The maintainer authorized committing and pushing the completed patch to existing
[PR #5](https://github.com/CryptoExplor/agentforge/pull/5) for local-agent review.
This revision is based on its previous remote head
`bd6bf59d6f3bdb8229cd9736ed58bcf680c37920`, retaining all four commits after the
restored local baseline `3986dd1`. No existing shared history is rewritten.

| Item | Review handoff |
|---|---|
| [PR #4](https://github.com/CryptoExplor/agentforge/pull/4) | Previously merged into `main` at `4aee54199e9c1376313c47d6562ccc03de491a02` |
| PR #5 | Open; review branch `arena/01a0af8f-agentforge` |
| PR #5 base | `arena/01a0af63-agentforge`, **not `main`**; not retargeted |
| Review revision | Fetch the current PR head and record its SHA; the PR handoff comment identifies the pushed commit |

The local agent's earlier 99-test result at `bd6bf59` does not cover these newer
changes. Use the commands in [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).
Local test evidence and earlier PostgreSQL/package/advisory results are labeled
separately from live CI. Publishing for review is not independent approval,
a merge or authorization to deploy. The human maintainer decides the eventual
PR base and merge; agents must not merge, close, force-push or self-approve.

## Blocked and deferred

- The requested external `scripts/activity_engine/` implementation and named
  databases are absent. Its six P0 proposals are reviewed, not implemented or
  verified here: [external-client review](ACTIVITY_ENGINE_P0_REVIEW.md).
- Independent local-agent audit and maintained-release PostgreSQL CI are pending.
  Human maintainer alone decides and performs merges.
- No broker, MCP, discovery subscription API, large SDK rewrite, external provider,
  OCI/Vercel deployment or agent pilot was performed.
- Outbox redaction is not audience authorization. Keep gossip disabled unless an
  explicitly approved audience policy protects private metadata.
- Existing SDK flat imports/methods remain supported (`list_tasks`, `get_task`;
  `client.tasks()` is not an existing method).

## Next gates, not execution authorization

Independent review → minimal compatible SDK/static documentation and correct
`llms` publication → protected staging → 5–10 ordinary-agent pilot → measured
10/25/50/100-agent progression. Registered agents are not concurrent clients;
100k+ remains a design horizon, not measured capacity.

The accepted [SDK](SDK_ARCHITECTURE_PLAN.md) and
[discovery scalability](DISCOVERY_SCALABILITY_PLAN.md) designs are retained,
not rolled back or implemented by this security follow-up. The
[integration boundaries](INTEGRATION_BOUNDARIES.md) remain binding. See
[PR plan](PR_PLAN.md) for the short review sequence.
