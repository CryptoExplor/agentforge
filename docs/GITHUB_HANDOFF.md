# GitHub handoff

## Start here

Read [AI_CONTEXT.md](../AI_CONTEXT.md), then
[PROJECT_STATUS.md](PROJECT_STATUS.md) and
[AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md). Those are the canonical current
context and evidence; this file deliberately does not repeat them.
Use [REPOSITORY_MAP.md](REPOSITORY_MAP.md) to find implementation surfaces.

## Review and publication workflow

1. Preserve the existing working tree and use only the session-assigned branch.
   Inspect `git status`, local HEAD, diff and live `gh pr view` before publication.
2. Distinguish the local tested patch from the remote PR head. Check the intended
   PR base with the maintainer; historical roadmap labels are not GitHub numbers.
3. Run the verification commands from the audit record. For changes to accounting,
   authorization or transactions, include disposable PostgreSQL coverage.
4. Provide the patch, exact results, compatibility changes, migration impact and
   unresolved limitations to the independent local/auditing agent.
5. Only publish/retarget when authorized. Never merge/close PRs, self-approve,
   force-push, push `main`, or treat a green check as merge permission.
6. The human maintainer alone decides and performs a merge. If an unexpected
   merge is observed, report it; do not rewrite history or autonomously revert.

No GitHub credentials should be requested or saved. Use the configured `git`/`gh`
connection; reconnect through Arena if authentication fails.

## Scope control

Use [PR_PLAN.md](PR_PLAN.md) for next gates and
[INTEGRATION_BOUNDARIES.md](INTEGRATION_BOUNDARIES.md) for ownership. External
client sources, provider keys, real-network adapters, large SDK changes and
infrastructure are not implied by an audit request. Do not invent missing files,
upstream contracts, live receipts or a deployment URL.
