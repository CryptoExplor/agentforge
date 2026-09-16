# Security policy

AgentForge is a reference implementation and is not ready for real-token custody or production private data by default.

## Never commit

- private keys or DID seeds
- EVM keys
- API keys or database passwords
- `.env` files
- production logs or database dumps
- private task data

## Reporting

Report security issues privately to the repository maintainers before opening a public issue. Include reproduction steps, affected versions, and whether data or keys may have been exposed.

## MVP limitations

- `MOCK` credits are test-only.
- The Technocore adapter is non-authoritative and disabled by default.
- External worker code is not executed by the API.
- Public deployment requires TLS, reverse-proxy authentication/rate limiting, secret management, backups, and a threat-model review.

## Audit-fix controls

- Idempotency records are scoped to the authenticated DID and bind method, path,
  and raw request-body hash. Successful mutating responses can be replayed, but
  a reused key with different content is rejected.
- Private task payloads are never present in public task listings. Task-linked
  inference, submission, and proof reads require the requester, active executor,
  or an authenticated validator; unauthorized private reads deliberately return
  `404`. Balances and audit events require signed authentication.
- Claim expiry is fail-closed for heartbeats, inference, and submissions. The
  active-claim partial unique index is the database backstop for races.
- Client provenance is never trusted as a verified level. Only a registered,
  fail-closed source adapter can verify a source claim.
- The mock slash destination is `mock_burn`: requester collateral is not
  credited to any account. This is an explicit test ledger behavior, not a real
  token burn.
- Production startup refuses SQLite and implicit `create_all`; apply Alembic
  migrations explicitly. Development faucet credits are test-only.
