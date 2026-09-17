# Contributing

Read the repository handoff before changing behavior:

1. `README.md`
2. `docs/PROJECT_STATUS.md`
3. `docs/AUDIT_VERIFICATION.md`
4. `docs/ARCHITECTURE_DECISIONS.md`
5. `docs/PR_PLAN.md`
6. `AGENTFORGE_ARCHITECTURE.md`
7. `ANTIGRAVITY_IMPLEMENTATION_BRIEF.md`

## Rules

- Keep protocol objects versioned and JSON-serializable.
- Do not add FLOP-specific behavior without an adapter and an official specification reference.
- Add tests for state transitions, signatures, replay protection, evidence hashes, authorization, and mock escrow invariants.
- Never add secrets or real private keys to fixtures.
- Keep external worker execution outside the API process.
- Keep deployment/network mode and activity eligibility server-derived.
- Do not add airdrop scoring, farming automation, or guessed external settlement rules.

## Review and merge authority

Do not merge. AgentForge follows a three-role workflow and only the human
maintainer performs merges:

```text
Web Agent (implementer)     implements the change, opens the PR, never merges
Local/auditing agent        audits, runs the verification commands, reports findings
Maintainer (human)          decides and performs the merge
```

- An agent must never merge a pull request, close a pull request, push to `main`,
  force-push, or self-approve. Agents work only on their own session branch and
  propose changes through a pull request.
- The auditing agent records findings as review comments, a report, or a
  follow-up commit on its own branch. It never marks a PR ready for merge on
  behalf of the maintainer.
- A merge requires an explicit human decision. Green CI, a clean audit, and
  "mergeable: true" are inputs to that decision, never a substitute for it.
- If an agent merged something by mistake, say so immediately and let the
  maintainer revert it (`git revert -m 1 <merge-commit>`) rather than rewriting
  shared history.
- Anyone reviewing this repository, including automated agents, should treat an
  unexpected merge commit as a process violation worth flagging.

## Before a pull request

```bash
python -m pytest -q
python -m compileall -q server sdk tests examples
python -m alembic upgrade head
```

When protocol schemas/routes change, also validate the JSON Schemas and regenerate/check `protocol/v1/openapi.json`. When migrations change, run an upgrade/downgrade/upgrade round trip against a disposable database. Run `git diff --check` before committing.

Use `.github/pull_request_template.md` and keep the change small enough for one focused review. The planned future branch/commit sequence is in `docs/PR_PLAN.md`.
