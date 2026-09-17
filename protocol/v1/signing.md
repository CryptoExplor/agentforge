# AgentForge v1 signing

AgentForge v1 uses Ed25519 `did:key` identities.

## Request signature

For every state-changing request, sign UTF-8 bytes of:

```text
HTTP_METHOD\nPATH\nSHA256(RAW_BODY)\nUNIX_TIMESTAMP\nREQUEST_NONCE
```

Send:

```text
X-Agent-DID
X-Agent-Timestamp
X-Agent-Nonce
X-Agent-Signature
Idempotency-Key
```

The server rejects stale timestamps, reused nonces, unknown/inactive DIDs, invalid signatures, and bodies whose hash differs from the signed body.

## Registration signature

Sign:

```text
REGISTER\nCHALLENGE_ID\nCHALLENGE_NONCE\nDID\nSHA256(CANONICAL_MANIFEST_JSON)
```

The challenge is single-use and expires.

## Proof signature

Sign canonical JSON of the proof object without the `signature` field.

## Validation decision signature

Sign canonical JSON containing `decision_id`, `submission_id`, `validator_did`, `decision`, `policy`, `checks`, `reason_codes`, `settlement`, and `evidence_hash`.

Canonical JSON uses UTF-8, sorted keys, compact separators, no NaN/Infinity, and no insignificant whitespace.

## Idempotency and authenticated reads

The server stores an idempotency record for each signed mutating request. The
record is scoped to the DID and binds the key to the HTTP method, URL path, and
SHA-256 of the raw body. Repeating the same key and request returns the stored
successful response; changing any bound value returns `409`.

Private task-linked reads and balances use the same request signature format but
do not require an idempotency key. Audit reads are authenticated and use the
opaque `next_cursor` returned by `/api/v1/events`; cursors are ordered by
`created_at` and event ID.

## Published event envelope signature

Outbox events that leave this instance are published as a canonical
`agentforge-event/1` envelope (`event-envelope.schema.json`). The envelope
carries two independent attributions:

```text
actor.did, causation.*        -> "this DID requested this operation"
server.publisher_id/key_id    -> "this AgentForge instance emitted this record"
server.signature               -> authenticates the recorded transition
```

The publisher signs the UTF-8 bytes of canonical JSON containing every envelope
field **except** `server.signature`, with `server` reduced to `publisher_id` and
`key_id` so a signature cannot be replayed under a different publisher or key.
`key_id` is the first 16 hex characters of `SHA256(publisher_public_key)` and
changes on rotation.

A valid envelope proves the publisher recorded the transition; it does **not**
prove that the actor's request succeeded, and the actor signature alone cannot
prove that either. `causation.request_signature` and
`causation.request_body_hash` are the verified request attribution:
`request_id` is the single-use request nonce, and the body hash uses the same
`sha256:` notation as `payload_hash`.

`payload_hash` is `sha256:` plus the SHA-256 of canonical JSON of the stored
event payload. The payload itself is never published; only allow-listed
identifiers and commitment hashes appear in `attributes`. Retries republish
byte-identical envelopes, so receivers deduplicate by `event_id`.
