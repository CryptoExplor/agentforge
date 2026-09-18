## Summary

<!-- What is the smallest problem this PR solves? -->

## Merge authority

- [ ] This PR is **not** self-merged. Only the human maintainer merges after audit.

## Scope

- [ ] This PR is one cohesive change.
- [ ] Explicit non-goals are listed below.
- [ ] No arbitrary external-agent code runs inside the API.
- [ ] No client-controlled eligibility or deployment/network mode was added.

## Verification

- [ ] `python -m pytest -q`
- [ ] `python -m compileall -q server sdk tests examples`
- [ ] JSON Schemas validated
- [ ] OpenAPI regenerated/checked when the public API changed
- [ ] Alembic upgrade/downgrade checked when migrations changed
- [ ] `git diff --check`

**Commands and results:**

```text
paste exact commands/results here
```

## Security and data handling

- [ ] Authorization reviewed for every new read/write path.
- [ ] Idempotency/replay behavior reviewed for every mutation.
- [ ] No secrets, private keys, private payloads, database dumps, or generated caches added.
- [ ] External receipts are verified before any external-network status is claimed.

## Protocol and migration impact

- Public API/schema changes: <!-- none / describe -->
- Migration changes: <!-- none / describe -->
- Rollback considerations: <!-- none / describe -->

## Explicit non-goals

<!-- Mention anything intentionally deferred, especially FLOP, TCLK, settlement, or airdrop behavior. -->

## Reviewer notes

<!-- Point reviewers to focused files/tests and any known environment limitations. -->
