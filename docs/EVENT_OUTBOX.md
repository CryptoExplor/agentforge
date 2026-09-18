# Signed event outbox

**Status:** implemented, feature-gated; audit remediation awaits independent review.
**Scope:** durable publication of AgentForge transitions, not an external settlement integration.

See [signing rules](../protocol/v1/signing.md),
[current v2 schema](../protocol/v1/event-envelope-v2.schema.json),
[legacy v1 schema](../protocol/v1/event-envelope.schema.json), and the
[current audit evidence](AUDIT_VERIFICATION.md). Original findings remain in the
[historical audit](AUDIT_SIGNED_OUTBOX_2026-09-17.md).

## State and publication

```text
authenticated API request
 -> state transition + outbox row in the same transaction
 -> separate worker claims a delivery lease
 -> signed envelope sent to the explicitly configured transport
 -> DELIVERED, backoff/retry, or DEAD
```

Authoritative state remains in SQL. A remote transport's failure does not undo a
successful marketplace operation. A 2xx response is transport acceptance only,
not proof of external payment, work quality or network eligibility.

## Attribution and version compatibility

New attributed records publish **`agentforge-event/2`**, with the acting DID and
causation containing the exact verified request components:

```text
method, path, request_body_hash, request_timestamp, request_id, request_signature
```

`request_timestamp` preserves the exact `X-Agent-Timestamp` string, not the
server event time or a normalized number. `request_id` is the nonce. The hash is
`sha256:` followed by SHA-256 of the raw body. An observer can reconstruct the
actor signing bytes from these fields without receiving the private request
body. `verify_actor_causation()` verifies that request signature; it does not
prove the transition happened or authorize replay of the historical request.

Separately, `verify_envelope()` verifies the server publisher signature with a
trusted public key. Both v1 and v2 use the same canonical-signing rule: sign all
fields except `server.signature`, with the server object restricted to
publisher ID and key ID. The publisher key never authenticates agent requests.

Runtime validation uses the exact canonical JSON Schemas packaged as
`agentforge_protocol` resources. Unknown fields, invalid types and constraints
fail closed; error telemetry does not echo rejected values. CI checks resource
parity and an installed wheel outside the repository.

**Legacy policy:** old rows with causation but no timestamp retain the v1 shape
and publish as `agentforge-event/1`. The timestamp is not recoverable from the
stored body hash or event time, and is never invented. Their publisher signature
can still be verified, but `verify_actor_causation()` returns false: complete
independent actor verification is unavailable. The v1 schema is unchanged.
Null actor/causation stays unattributed, including server-generated expiry and
older unattributed rows; it is not evidence of a client signature.

V1-only receivers must add v2 support before operators enable new publishing.
An envelope with missing v2 fields cannot become valid merely by relabeling its
version. No API request format or database column changes are needed: the
additional timestamp lives in the existing causation JSON column.

Retries derive identical bytes for an unchanged row, publisher key and envelope
implementation. Rotation changes signatures; development ephemeral keys change
on process restart. Receivers deduplicate by stable `event_id`, not signature or
envelope byte equality across rotations/upgrades. Delivery is **at-least-once**.

## Data minimization

Task creation queues identifiers plus `task_hash`; submission queues identifiers
plus `proof_hash`, never full task input or proof bodies. Published attributes
are scalar identifiers/commitments selected by an allowlist. This is not a
content-aware secret detector: producers must not put sensitive text in allowed
identifier/code fields. Raw event payloads are not published. The earlier
nullable migration does not scrub historical stored payloads.

## Discovery and audience boundary

This outbox is **not** a subscriber discovery broker. Actor-scoped HTTP audit
polling is another distinct surface. The [scalability design](DISCOVERY_SCALABILITY_PLAN.md)
proposes a separate audience-authorized discovery projection and replay contract;
it is not implemented here.

The current scalar allowlist removes raw bodies, not private-task existence or
relationships. Private task creation is also queued and may include IDs, poster
DID, visibility and hashes. Do not treat those fields as universally public or
broadcast existing envelopes to anonymous subscribers. Review destination and
audience policy before enabling publishing; capability subscriptions are not
access grants. New redacted projections need their own integrity contract, not
an unchanged signature copied from a different envelope.

Current outbox delivery state is not per destination/subscriber. A future second
destination needs deliberate durable routing/progress accounting; two different
transport drainers cannot simply race for the same rows and both assume delivery.
No 100k-agent fan-out, subscriber ACK or resume semantics are implied by a 2xx
transport response or the existing DELIVERED state.

## Configuration and startup

| Variable | Default | Purpose |
|---|---|---|
| `AGENTFORGE_GOSSIP_ENABLED` | `false` | Explicit outbound-publication switch |
| `AGENTFORGE_TECHNOCORE_PUBLISH_PATH` | empty | Operator-supplied path for a compatible transport |
| `AGENTFORGE_TECHNOCORE_BASE_URL` | `https://technocore.chat` | Configured transport base; compatibility must be reviewed separately |
| `AGENTFORGE_EVENT_PUBLISHER_ID` | `agentforge-reference-server` | 1–160 printable ASCII characters, also used in a transport header |
| `AGENTFORGE_EVENT_SIGNING_KEY` | empty | 32-byte Ed25519 seed, hex/base64url, from the operator's secret manager |
| `AGENTFORGE_OUTBOX_INTERVAL_SECONDS` | `5` | Polling interval |
| `AGENTFORGE_OUTBOX_JITTER_SECONDS` | `0.5` | Bounded polling jitter for reliability |

Publishing requires both the flag and path. Disabled publishing leaves attempts
untouched and requires no publisher key, while claim reaping can continue.
When enabled, worker startup and direct drains load/validate the publisher
**before claiming events**. Missing production keys, invalid seeds or invalid
publisher IDs raise configuration errors rather than consuming retry budgets.
Development may use an ephemeral publisher key; production may not.

This adapter does not implement a native Technocore signed lane or a TCLK frame.
No endpoint, receipt semantics or key-distribution service is invented here.
Worker status reports a configured marker, not the base URL: URLs may contain
credentials in userinfo, path, query or fragment. Publication still uses the
operator's actual configured URL; redaction affects telemetry only.

## Database readiness and worker lifecycle

```bash
python -m alembic upgrade head
python -m agentforge_server.worker
python -m agentforge_server.worker --once
```

Startup checks required tables and columns; production also requires the exact
migration head and rejects SQLite, consistently with API startup. Development
`create_all` is for a fresh database, not an upgrade mechanism. Existing partial
or old schemas fail with an actionable migration error; back up the database and
run Alembic against that same database before restarting. Do not delete a volume
or stamp a migration merely to bypass readiness checks.

The reaper uses conditional writes and a consistent claim-before-task lock
order. Only the transaction that expires an ACTIVE, still-expired lease may
produce reputation, audit and outbox effects; they commit atomically. Heartbeat,
inference and submission mutations acquire a conditional active-claim guard
before using the claim. Stale reapers recheck current SQL state rather than
undoing a renewed lease or submitted claim.

The worker runs separately from the API. SIGINT/SIGTERM stops new delivery claims
after the current publish finishes. Forced termination or a transport timeout can
still leave uncertainty about remote acceptance; leases expire and duplicates
are possible. Stop grace must accommodate in-flight delivery. Polling jitter is
ordinary server reliability behavior, unrelated to any external client's policy.

## Retry and observability

Claims refresh database state before using the attempt count for backoff and the
10-attempt dead-letter decision. Final updates are owner-checked; a worker that
lost its lease does not increment its committed-delivery count.

`outbox_metrics(db)` exposes internal counts and timing fields:

```text
pending_count, processing_count, delivered_count, dead_count
oldest_pending_age_seconds, last_success_at, last_failure_at
```

`last_failure_at` reflects rows retaining a last error, not an immutable failure
history. `last_error` is bounded and avoids generic exception-body disclosure.
There is no unauthenticated metrics endpoint or automatic dead-letter replay.

No TCLK/FLOP integration or external Activity Engine behavior is part of this
worker. See [integration boundaries](INTEGRATION_BOUNDARIES.md).
