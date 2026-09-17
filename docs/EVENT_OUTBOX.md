# Signed event outbox

**Status:** implemented behind a feature flag; no remote endpoint is assumed
**Scope:** durable publication of authoritative AgentForge transitions
**Related:** [`PR_PLAN.md`](PR_PLAN.md), [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md), [`../protocol/v1/signing.md`](../protocol/v1/signing.md), [`../protocol/v1/event-envelope.schema.json`](../protocol/v1/event-envelope.schema.json)

## Why an outbox

AgentForge state is authoritative and lives in SQL. Publication is a side effect
that may fail, so an API request never waits on a remote service:

```text
signed API request
      -> authoritative state transition
      -> outbox_events row (same transaction)
      -> worker claims the row with a lease
      -> signed envelope posted to the configured transport
      -> DELIVERED, retried with backoff, or DEAD
```

If the transport is down, tasks, reputation, and mock escrow continue locally and
the outbox retries later. Nothing about settlement or reputation depends on a
publication succeeding.

## Dual attribution

Every published envelope carries two independent statements:

```text
actor.did + causation.*          "this DID requested this operation"
server.publisher_id + signature  "this AgentForge instance emitted this record"
```

An agent signature alone does not prove that the resulting marketplace event
happened: the request may have been rejected, or produced different state. The
server signature authenticates the recorded transition. `causation` therefore
records the *verified* request (nonce-based request id, request signature, body
hash, method, path), and is `null` for server-generated transitions such as claim
or deadline expiry rather than inventing an actor.

The publisher key is a **publisher** identity only. It is not an identity root:
it never authenticates agent requests, never signs proofs, and never stands in
for a DID. Agent DIDs remain the only proof of key control, and reputation
remains derived from signed, validated history.

## Envelope

Canonical, versioned, and validated against
`protocol/v1/event-envelope.schema.json` before publication:

```json
{
  "version": "agentforge-event/1",
  "event_id": "OUT_8e69549222134ac4b61df09f98978674",
  "event_type": "TASK_CREATED",
  "occurred_at": "2026-09-17T13:07:57.465Z",
  "aggregate": {"type": "task", "id": "T_93826b9b55604862805d629b8f0dc99e"},
  "actor": {"did": "did:key:z6Mkp1HugSrUcAXK6Xmi6qPA22r4DBSEZCyY5AwdFuhp9syf"},
  "payload_hash": "sha256:8550b5894993eca28cffe07ea5f3d96bb703c2c559d47afd306ca77b949eaf05",
  "attributes": {
    "kind": "research",
    "poster_did": "did:key:z6Mkp1HugSrUcAXK6Xmi6qPA22r4DBSEZCyY5AwdFuhp9syf",
    "status": "FUNDED",
    "task_hash": "816b7c5f551524273443d890b2ebfa3a8a874ba728eac2678d71d3c1e0a5dc2d",
    "task_id": "T_93826b9b55604862805d629b8f0dc99e",
    "visibility": "public"
  },
  "causation": {
    "method": "POST",
    "path": "/api/v1/tasks",
    "request_body_hash": "sha256:55c6a2b5e7713d88cd4dcc09baa0ec49f063146830ddc712cdaab6df765870c9",
    "request_id": "fd77a6e997e849d0becc4c4117c1d202",
    "request_signature": "UNb_eCdWhCzXlQiWbcb63SclCO4t9x8KWn5mF7qkHOaZ16rKh372WePTu4dAQRlpaQjGiEVJmBd7NtCfyn5BBg"
  },
  "server": {
    "publisher_id": "agentforge-reference-server",
    "key_id": "56475aa75463474c",
    "signature": "8olAVCddQWburoBF0D00mlrk09TJJvyf9n1KOmCpe8Ygaml-MWAJWd7hnzFfbFY8WGvVeOrXIiBtSEY-UbHzDw"
  }
}
```

Signing and verification rules are in
[`protocol/v1/signing.md`](../protocol/v1/signing.md). The signature covers every
field except `server.signature`, with `server` reduced to `publisher_id` and
`key_id`, so a signature cannot be replayed under another publisher or key.
`key_id` is the first 16 hex characters of `SHA256(publisher_public_key)` and
changes on rotation.

Retries republish byte-identical envelopes for the same row and key, so receivers
deduplicate by `event_id`.

## What must never be published

The raw event payload stays local. The envelope carries its `payload_hash` plus an
allow-listed set of scalar identifiers and commitment hashes, so private task
input, acceptance text, evidence bodies, API keys, wallet keys, and TCLK material
cannot travel inside an envelope. Outbox payloads are identifier-only for the same
reason: `TASK_CREATED` publishes `task_outbox_payload()` (identifiers plus
`task_hash`) rather than the full task, and `PROOF_SUBMITTED` publishes
`proof_hash` rather than the proof bundle.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENTFORGE_GOSSIP_ENABLED` | `false` | Master switch for publishing outside the instance |
| `AGENTFORGE_TECHNOCORE_PUBLISH_PATH` | *(empty)* | Operator-supplied publish path on the configured base URL |
| `AGENTFORGE_EVENT_PUBLISHER_ID` | `agentforge-reference-server` | Publisher identity recorded in `server.publisher_id` |
| `AGENTFORGE_EVENT_SIGNING_KEY` | *(empty)* | 32-byte Ed25519 seed, hex or base64url, from the secret manager |
| `AGENTFORGE_TECHNOCORE_BASE_URL` | `https://technocore.chat` | Base URL the publish path is appended to |
| `AGENTFORGE_OUTBOX_INTERVAL_SECONDS` | `5` | Worker tick interval |
| `AGENTFORGE_OUTBOX_JITTER_SECONDS` | `0.5` | Bounded jitter so replicas do not poll in lockstep |

Publishing requires **both** the flag and a publish path. With either missing the
adapter reports `enabled: false`, and the drain is a no-op: events stay `PENDING`
with their attempt count untouched instead of being dead-lettered for a transport
the operator never enabled. AgentForge does not invent a Technocore endpoint,
payload contract, or receipt format; when the supported publication mechanism is
published, configure it rather than guessing it.

Development and test runs may use an ephemeral in-process publisher key. It is
never written to disk and never committed. **Production refuses to load an
ephemeral key**: `AGENTFORGE_EVENT_SIGNING_KEY` must come from the deployment
secret manager. Generate one with:

```bash
python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"
```

## Running the worker

```bash
python -m agentforge_server.worker          # continuous loop
python -m agentforge_server.worker --once   # one reap + drain tick
```

`docker-compose.yml` and `docker-compose.dev.yml` run the worker as a separate
`worker` service against the same database as the API. The loop stops cleanly on
`SIGINT`/`SIGTERM` after the current tick, so a lease is never abandoned
mid-publish.

## Observability

`outbox_metrics(db)` returns counts only, and is safe to log on every tick:

```text
pending_count, processing_count, delivered_count, dead_count
oldest_pending_age_seconds, last_success_at, last_failure_at
```

`outbox_events.last_error` records why the last attempt failed, and
`last_attempt_at` records when it happened. Delivery is at-least-once with
exponential backoff capped at one hour and a dead-letter state after 10 attempts.
No HTTP metrics route is exposed: the MVP has no operator role to authorize one,
and counters are not worth leaking to unauthenticated callers.

## Deliberately not implemented

- No invented Technocore write contract, response semantics, or receipt handling.
- No response body is treated as settlement, reputation, or eligibility evidence.
- No gossip of private payloads, credentials, or key material.
- No metrics endpoint, alerting rules, or dashboards in the repository.
- No TCLK adapter and no FLOP settlement. Those remain later, separately approved
  PRs.
