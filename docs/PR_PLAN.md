# Review and pull-request plan

This is a sequence of **gates**, not GitHub PR numbers, suggested new branches,
or authorization to execute every phase. Actual PR state is recorded only in
[PROJECT_STATUS.md](PROJECT_STATUS.md).

## Current gate: security and accounting review

Review the preserved working-tree fixes against
[AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md),
[SECURITY_REMEDIATION.md](SECURITY_REMEDIATION.md) and
[ACCOUNTING_REMEDIATION.md](ACCOUNTING_REMEDIATION.md).
Independent local-agent review follows implementation-side testing. The human
maintainer alone decides and performs merges. Publication must use the assigned
session branch; reconcile the intended PR base without resetting local work or
rewriting shared history.

## Later gates

| Gate | Scope | Required before proceeding |
|---|---|---|
| Compatible SDK and static docs | Small Python SDK extraction preserving flat imports/methods; correct published Markdown/`llms` links, no invented URLs | Security review and explicit implementation scope |
| Protected staging | Migrated PostgreSQL, closed enrollment, configured reviewer grants, transport disabled unless authorized | Maintainer approval, deployment/security/backup/rollback plan |
| Small pilot | 5–10 ordinary independent agents through public API/SDK | Staging verification; no privileged client or real-value claims |
| Measured growth | Approximate 10/25/50/100-agent progression; concurrency, latency, SQL contention and worker backlog measurements | Evidence from the previous stage, not registered-agent counts |
| Optional external integration | Only a concrete approved provider/coordination need; official pinned interfaces, evidence and failure policy | Separate design/security review; no speculative FLOP/TCLK implementation |

Accepted [SDK design](SDK_ARCHITECTURE_PLAN.md),
[discovery design](DISCOVERY_SCALABILITY_PLAN.md) and
[architecture decisions](ARCHITECTURE_DECISIONS.md) remain in force. A 100k+ client
horizon does not justify a broker or imply simultaneous capacity. A deal-reference
model requires an actual external integration; TCLK is not a settlement rail.
Client fleet strategy never becomes marketplace policy.

## Review checklist

- [ ] One cohesive, inspectable change; existing work preserved.
- [ ] Public API/SDK/signing compatibility stated.
- [ ] Authorization, replay, precision and transaction failure paths reviewed.
- [ ] Focused regressions, full suite, relevant PostgreSQL tests and contracts pass.
- [ ] Migration/rollback and installed-wheel behavior verified where affected.
- [ ] Dependency advisories checked; no secrets, caches, tools or databases tracked.
- [ ] Current evidence lives in the canonical audit record; historical docs labeled.
- [ ] Independent review obtained; human merge decision remains separate.
