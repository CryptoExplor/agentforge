# D1–D6 security remediation and audit handoff

**Updated:** 2026-09-18 (Asia/Calcutta). **Status:** implemented in the working tree; independent
review and production/OCI verification pending. No deployment, broker, MCP,
discovery API or SDK refactor is included. This record supersedes the OPEN
implementation status in the dated readiness/design documents, not their
public-launch prohibition or historical evidence.

> **Accounting follow-up:** the lost-update and precision findings are now fixed
> with lifecycle regressions. See [accounting remediation](ACCOUNTING_REMEDIATION.md).
> Current results and dependency evidence are maintained only in
> [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md); independent approval is pending.

## Implemented controls

| Finding | Change | Main regression coverage |
|---|---|---|
| D1 | Empty-by-default operator allowlist AND registered validation capability for reviewer private reads and validation/dispute decisions. Current approval is required even on decision idempotency replay. Requester/current linked executor access remains. | Self-declared `validation`/`validator` cannot read any of task/inference/submission/proof; legitimate access; revocation; capability removal; denied decisions; replay cannot bypass revocation. |
| D2 | Bounded acceptance-schema subset, only local acyclic JSON Pointer references; explicit non-retrieving registry; no regex, dynamic refs or combinator expansion. Task creation rejects unsafe schemas; legacy stored schemas fail validation closed. | HTTP/file/relative references never fetch; cyclic/unsupported schemas reject; local refs work; boolean false enforced; private error values not echoed; size/depth bounds. |
| D3 | ASGI middleware buffers at most 2,000,000 received bytes before JSON parsing/authentication; bounds body time, headers and request target; validates framing and preserves exact body bytes. | Missing/false Content-Length and chunked bypass; malformed/duplicate length; disconnect; timeout; boundary bytes; a genuinely signed noncanonical chunked request still succeeds. |
| D4 | SQL-atomic fixed-window global/peer/registration/verified-DID admission, per-process in-flight cap, production enrollment closed by default and production faucet forbidden. Atomic challenge consumption. Bounded listing responses and security-state pruning. | Shared quotas across app instances; concurrent admission; denial commits; unsigned DID cannot charge victim; fail-closed DB errors; registration closure; challenge race; retention; concurrency recovery; additive migration. |
| D5 | Register static agent search before dynamic DID route; bounded search and capability aggregation. | Search returns actual agents, preserves DID lookup and enforces limit. |
| D6 | Reward filter length, finite/nonnegative value and exponent checks return 422, not 500. Also bound monetary/compute exponent before formatting to prevent enormous decimal expansion. | Invalid/NaN/infinite/negative/huge filters; valid decimal/scientific inputs; short exponent-expansion attacks; request-target limit. |

Sources (updated for the Phase 1.5 modular decomposition — the D1–D6 request
handlers moved out of the former monolithic `app.py` into per-domain routers):

- **D1** private-read / validator authorization — `routes/_shared.py`
  (`authorize_task_read`, the operator/validator gate) applied by
  `routes/tasks.py`, `routes/submissions.py`, `routes/validations.py` and
  `routes/disputes.py`; registry logic in `operators.py`.
- **D2** bounded acceptance-schema subset — `validators/result_schema.py`,
  enforced at task creation in `routes/tasks.py`.
- **D3** received-byte / body / header / target limits — `middleware.py`
  (`RequestSecurityMiddleware`, mounted by the `app.py` composition root).
- **D4** SQL-atomic shared admission quotas — `admission.py`, `settings.py`,
  `worker.py` (security-state pruning).
- **D5** static `agents/search` registered before dynamic `agents/{did}` —
  `routes/agents.py`, with mount order fixed once in `routes/__init__.py`.
- **D6** bounded decimal reward-filter parsing (returns `422`) — `routes/tasks.py`
  (`min_reward` handling) plus `money.py` and `schemas.py`.

Shared: `models.py` (schema), `validators/deterministic.py`. The composition
root `app.py` now only wires middleware and mounts `routes.DOMAIN_ROUTERS`.
Tests: `tests/test_public_exposure.py`. Existing positive validator fixtures now
make an explicit operator grant; they do not weaken the application policy.

## Validator authorization and migration of existing deployments

`AGENTFORGE_TRUSTED_VALIDATOR_DIDS` is a comma-separated list of **public**
Ed25519 did:key identities. Empty means no approved reviewers, in all environments.
Registration, re-registration and self-advertised capabilities cannot modify it.
Reviewers must also be active registered identities with validation capability;
existing independence checks still apply to decisions. Operators validate this
configuration at startup. Settings are process configuration: apply grant/revoke
changes consistently to **all API replicas** and restart/reload the deployment.
There is no new public admin endpoint or automatic trust-grant mechanism.

This deliberately small policy gives approved reviewers access across this
instance's private tasks. It is **not** task-scoped assignment or tenant RBAC;
only enroll identities trusted with that privilege. Per-task delegation needs a
separate design/audit. Owners/current linked executors retain ordinary access.
Previously unapproved validators will receive 404 on private reads and 403 on
decisions, even if they worked before; insecure authorization is not retained
for compatibility. The SDK method and signing formats are unchanged.

## Admission defaults and guarantees

| Setting | Default | Scope |
|---|---|---|
| `AGENTFORGE_REGISTRATION_OPEN` | false in production; true otherwise | New identities only; existing identities can update manifests |
| `AGENTFORGE_REQUEST_GLOBAL_PER_MINUTE` | 6000 | All `/api/v1/` requests reaching admission |
| `AGENTFORGE_REQUEST_IP_PER_MINUTE` | 600 | ASGI peer address |
| `AGENTFORGE_REGISTRATION_GLOBAL_PER_MINUTE` | 120 | Combined challenge GET + registration POST traffic |
| `AGENTFORGE_REGISTRATION_IP_PER_MINUTE` | 30 | Same registration traffic per peer |
| `AGENTFORGE_REQUEST_DID_PER_MINUTE` | 300 | Only after signature/active identity verification |
| `AGENTFORGE_MAX_INFLIGHT_REQUESTS` | 32 | Concurrent HTTP requests per API process |
| `AGENTFORGE_BODY_TIMEOUT_SECONDS` | 10 | Total request body receive deadline |

Rate limits must be positive bounded integers; there is no disable-on-error path.
The fixed-minute window can admit up to twice a limit near a boundary; it is not
a sliding-window guarantee. Global counters intentionally serialize admission
for the small hosted MVP, not 100k-client infrastructure. Registration usually
costs two requests, so the default peer registration quota allows about 15 new
identities/minute. Size these settings only after measuring a protected pilot.

Counts are shared in SQL across API processes. Global admission occurs before
identity-controlled quota allocation. Earlier quota charges survive a later
quota rejection; signed admission commits before business writes, so failed
mutations do not erase their charge. The authenticated quota reuses the request
session to avoid holding one pooled connection while waiting for another.
Only the verified DID is charged; a claimed header alone grants no quota identity.

The middleware reads the ASGI peer, not `X-Forwarded-For`. Uvicorn/proxy deployment
must trust **only known proxy addresses**, never an arbitrary forwarded-header
allowlist or `*`. Behind a proxy, misconfiguration can either aggregate all users
under one peer or let attackers spoof peers. Edge rate/concurrency/connection
limits, TLS, finite DB pools and synchronized host clocks remain required.
Application throttling cannot stop network saturation or every database DoS.

429 responses include Retry-After; admission failure returns generic 503 rather
than bypassing controls or leaking database exceptions. Body errors use 400/408/
413/415; excessive request target/header sizes use 414/431. API responses carry
`Cache-Control: no-store`; configure proxies/CDNs not to override it. Clients
should back off with jitter, not blindly retry mutations with new operation keys.

Security counters retain current/previous windows. The worker removes expired
challenges after a grace period and nonces only after **twice the maximum
supported clock skew (3600 seconds), plus 60 seconds**, regardless of its local
skew setting. This protects differing API settings during rolling changes.
Nonce pruning must not permit an otherwise-fresh replay. Business idempotency,
audit, ledger and outbox data are **not silently expired**; backup/archival and
capacity policies for them remain operator work. Enrollment closure and rate
limits bound growth rate, not total lifetime storage.

## Server time is the only time authority (Grok roadmap 1.4)

A client timestamp is an unauthenticated claim, so it never starts a lease,
extends a lease, or decides a deadline. Details, settings and limits live in
[server time and clock drift](SERVER_TIME_AND_CLOCK_DRIFT.md); the security
consequences are:

- The signed-request drift window is **60 seconds** either side of the server's
  own receipt time (`AGENTFORGE_REQUEST_CLOCK_SKEW_SECONDS`), down from 300.
  Outside it the request is refused with `401 "client clock drift exceeds
  tolerance"`, consumes no nonce and creates no state. The window is validated
  at startup, so it cannot be misconfigured to zero, negative or unbounded.
- Every request is stamped with `received_at` at ingress from a
  **monotonic-anchored** server clock. A wall-clock step backwards cannot rewind
  an in-flight lease or re-open an expired one; a suspended host resynchronises
  forward only, so honest clients are not rejected as futuristic.
- Claim leases are only ever `received_at + lease`. A spoofed
  `X-Agent-Timestamp` therefore cannot lengthen an execution lease, repeated
  heartbeats cannot accumulate lease time, and a slow handler cannot gain any.
- Submission deadlines and the bounded dispute window are decided against
  **database server time**, cross-checked against the API host clock. If the two
  disagree beyond `AGENTFORGE_DB_CLOCK_SKEW_TOLERANCE_SECONDS`, the decision
  fails closed with `503` rather than settling on an ambiguous clock.
- `submissions.created_at` remains the executor-declared instant inside the
  signed proof, but it no longer decides anything: a back-dated proof cannot make
  a late submission look early.

The drift window bounds *when a request may be accepted*, not whether its content
is true, and the guards detect clock divergence without repairing it: NTP on the
API hosts and the database server is still required.

Listings retain their JSON shapes, but are bounded snapshots, not exhaustive
catalogues: agent search has limit <=100 and scans <=500 candidates; capability
aggregation returns <=100 names. Task listing filters visibility/status/origin
in SQL then scans <=500 candidates for remaining filters. Agent/task candidates
are streamed one at a time; returned item JSON is capped at 4 MB plus response
framing. A limit is an upper bound; fewer results may be returned. These are not
new discovery pagination/replay guarantees.

## Acceptance-schema subset

Supported: draft 2020-12 type/properties/required/additionalProperties/items,
min/max item/string/property counts, numeric bounds/multipleOf, enum/const,
$defs and acyclic local JSON Pointer $ref; descriptive title/description/comment.
The supported `$schema` declaration is permitted **only at the schema root**.
A nested declaration can make jsonschema select its ordinary validator instead
of the budget-enforcing wrapper; it now rejects even if it names the same draft.
Literal `$schema` keys inside const/enum instance data remain valid.
No pattern/patternProperties, remote/file references, $id/dynamic refs, format,
allOf/anyOf/oneOf/not/if, contains or uniqueItems. Unsupported keywords reject
rather than being silently ignored. Limits: schema <=16 KiB, <=512 structural
nodes, depth <=16, and <=512 expanded schema visits; validation input <=256 KiB,
<=4096 nodes, depth <=32; object <=128 entries, array <=256 entries; <=4096
keyword evaluations. This is deliberately not unrestricted JSON Schema execution.

New tasks with unsafe acceptance schemas return 422. Legacy unsafe stored
schemas fail deterministic validation; do not auto-accept them or mutate frozen
acceptance criteria. Resolve affected tasks through reviewed operational policy.
Error details no longer echo rejected result values or schema exception bodies.
Decimal amounts/compute remain strings but must normalize within 80 characters;
exponents are checked before allocating expanded representations.

## Migration, verification and remaining gates

Alembic head is now **`b0c9d8e7f6a5`** (server-anchored `received_at`), following
`a9b8c7d6e5f4` (platform fee), `f8a9b0c1d2e3` (operator registry),
`e7f8a9b0c1d2` (task verification strategy), `d6e7f8a9b0c1` (request quotas) and
`c4d5e6f7a8b9`. The Phase 1.1 column is additive with
`server_default='peer_review'`, so existing task rows are backfilled with the
manual strategy and no marketplace/outbox row is rewritten. The Phase 1.4
columns are additive `NOT NULL` floats backfilled from the already server-written
`created_at`; downgrade drops them and preserves the rows.
The additive `request_quotas` table/index and nonce/challenge cleanup indexes
do not change marketplace/outbox rows. Apply migrations in a controlled step before starting the new API/worker.
Old schema startup fails closed. Downgrade removes admission counters and these cleanup indexes;
stop the new services before downgrading and expect counters to reset on a
later re-upgrade. Business idempotency and outbox history survive.

## Verification and remaining gates

See [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md) for current results, exact
commands, PostgreSQL provenance, installed-wheel checks and dependency scanning.
Do not infer current evidence from the dated readiness or outbox audit snapshots.

The nested `$schema` budget bypass was fixed by allowing supported dialect
declarations only at the root; literal instance data and root declarations remain
supported. Operator grants, replica consistency, quota contention, malformed
inputs, migration/rollback and private metadata still need independent review.

The outbox is not audience-authorized discovery; keep gossip off without an
approved recipient policy. These controls are not a public-launch, private-data,
real-settlement, OCI capacity, load, backup or deployment certification.
