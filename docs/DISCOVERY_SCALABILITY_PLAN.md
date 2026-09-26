# Discovery and scalability contract — toward 100k+ agent clients

**Implementation update:** D1–D6 now have working-tree remediation and regressions;
see [security remediation / audit handoff](SECURITY_REMEDIATION.md). References
to OPEN/unimplemented security work below describe the earlier design snapshot.
Independent review and public-release gates remain unmet. SDK/discovery/hosting
proposals are still unimplemented.

**Date:** 2026-09-17 (Asia/Calcutta)
**Implementation inspected:** `bd6bf59d6f3bdb8229cd9736ed58bcf680c37920`
**Status:** `[PLANNED]` — design requirement and proposed interfaces only. No
broker, event API, SSE/WebSocket endpoint, SDK subscription method, schema,
migration, capacity benchmark or deployment is implemented by this document.

> [!NOTE]
> The current-behaviour tables below reference the pre-Phase-1.5 monolith by
> `app.py:NNN` line numbers. Those handlers now live in the domain routers:
> `GET /api/v1/tasks` and `GET /api/v1/tasks/{task_id}` (and their reaper hook)
> in `server/agentforge_server/routes/tasks.py`; `GET /api/v1/events` in
> `server/agentforge_server/routes/system.py`; the outbox payload builders in
> `server/agentforge_server/services.py`. The proposed discovery projection,
> broker and subscription designs remain **`[PLANNED]`** and unbuilt.

Read with [SDK architecture](SDK_ARCHITECTURE_PLAN.md),
[deployment readiness](DEPLOYMENT_READINESS_2026-09-17.md) and
[current outbox behavior](EVENT_OUTBOX.md). Current security/accounting progress
belongs in [PROJECT_STATUS.md](PROJECT_STATUS.md). This plan does not authorize
executing all its phases.

## 1. Decision and corrections to the proposed architecture

Adopt **event-assisted task discovery with selective routing and local client
filtering** as the long-term scaling direction. Preserve existing REST clients.
Avoid a design that requires every agent to poll the entire task catalogue.
Keep task, claim, proof and settlement authority in the marketplace; notification
transport must not determine business outcomes.

```text
committed task transition + transactional outbox
                   |
        authorized discovery projection
                   |
   durable replay store / routing index (future)
                   |
      replaceable delivery gateway/transport
                   |
       capability/topic routing + local filter
                   |
         API task fetch (signed when required)
                   |
      ordinary signed claim, subject to contention
```

The target is **100k+ agent clients**, not a claim that the current MVP supports
100k concurrent connections, an assumed 100k-agent private fleet, or a capacity
promise for one VPS. AgentForge remains neutral. Client budgets, scheduling,
interests and any Activity Engine behavior stay outside marketplace core.

Corrections worth retaining:

- Broadcasting every event to everyone and filtering only afterward still costs
  O(events × subscribers) network delivery. Prefer bounded topic/capability
  routing plus local policy filtering; measure hot-topic fan-out.
- Private task IDs, deadlines, rewards, capabilities, actor DIDs, hashes and even
  topic names can disclose information. **Metadata is not automatically safe.**
- Existing `/api/v1/events` is actor-scoped audit polling; it is not task discovery.
  The signed outbox is durable outbound publication, not a subscriber broker.
- A VPS is one deployment option. A container PaaS supporting a continuous worker
  and external PostgreSQL meets the same architectural need. No vendor is required.
- 100k live SSE/WebSocket subscribers require connection capacity somewhere,
  even when gateways keep those connections away from the transaction API.
- The quoted SDK example omitted identity: current `AgentForgeClient` requires
  `base_url` **and** `identity`. Hosting portability does not remove authentication.
- Example manifest fields `domains`, `languages`, `max_budget` and event kinds
  `task.updated`/`task.expiring` are not implemented contracts. Current manifests
  have capabilities/chains and reject unknown top-level fields. Local budget
  preferences do not need to become protocol fields. Monetary wire values use
  decimal strings with an asset, not floating-point budgets.

## 2. What exists, and what is missing

| Surface | Actual behavior | Not provided today |
|---|---|---|
| GET `/api/v1/tasks` | Public listing, capability/chain/status/origin/reward filters, response limit <=100; fetches latest 500 tasks before Python-side filtering; invokes reaper (`app.py:620–654`) | Complete paginated catalogue, durable change cursor, notification subscription, indexed routing |
| GET `/api/v1/tasks/{task_id}` | Public read or shared private authorization; invokes reaper (`app.py:656–663`) | A grant obtained by receiving an event; D1 private authorization is still flawed |
| GET `/api/v1/events` | Signed, actor-scoped `AuditEvent` read; cursor uses created time and ID (`app.py:1275–1327`) | Public task feed, all relevant events for a participant, subscriber ACKs or durable fan-out semantics |
| Python SDK `events()` | Synchronous HTTP audit polling (`client.py:303–309`) | `subscribe`, `acknowledge`, `resume`, `watch`, or a discovery namespace |
| SQL outbox / worker | Transactional source rows, leased retries, optional Technocore publication | Per-subscriber cursors, topic ACLs, replay retention, flow control, per-destination delivery accounting |
| Signed event v2 / legacy v1 | Publisher/actor attribution, commitment hash and allowlisted scalar attributes | A safe public routing projection with capability/chain arrays, task revision and complete snapshot/replay contract |
| Internal task versions | `Task.state_version` exists; some claim/expiry paths increment it; `task_payload()` omits it | A public monotonic revision maintained on every discovery-relevant transition; `task.version` is not a substitute |

Sources: `services.py:411–453`, `event_envelope.py:43–105`, `models.py:107`,
`outbox.py`, `worker.py`, `protocol/v1/event-envelope*.schema.json`.
Current kinds include `TASK_CREATED`, `TASK_CLAIMED`, `TASK_CANCELLED`,
`TASK_EXPIRED`, `CLAIM_EXPIRED`, `PROOF_SUBMITTED`, `VALIDATION_RECORDED`,
`DISPUTE_OPENED`; they do not establish a generic update or expiring-soon feed.

### Existing publication needs an audience review before fan-out

Task creation queues an outbox event for private as well as public tasks
(`app.py:596–601`). `task_outbox_payload()` includes task ID, poster DID,
visibility and task hash; other events also carry participant identifiers.
The publisher's scalar allowlist is not an audience authorization check.
Do **not** wire the raw signed outbox to an anonymous broker or browser feed.
Keep gossip disabled until destination/audience policy is reviewed. This is an
additional discovery/publication design gate, separate from D1–D6 remediation.
A derived redacted announcement cannot keep the original envelope's signature
as if it authenticated different bytes; use a separately specified projection
and integrity mechanism. Do not forward actor causation merely for discovery.

## 3. Security and data contract (required before release)

1. **Public projection:** only tasks explicitly public may yield public
   announcements. Candidate fields are source/environment identity, stable
   announcement ID, task ID, committed task revision, availability category,
   required capabilities/chains and explicitly approved public deadline/reward
   metadata. Each field must have a disclosure rationale, size limit and schema.
   This is a field design, not a new JSON schema or reserved naming/version.
2. **Private default:** emit nothing to public topics, including existence,
   identity, commitments, timing or tombstone reasons for a never-public task.
   Future private channels require server-controlled task/group membership.
   Self-declared capability or subscription to a topic is never a grant.
3. **Policy throughout:** check audience at projection, subscription, live
   delivery and replay. Recheck/revoke long-lived authorization; an old cursor
   or cached event cannot bypass a changed grant. Scope replay tokens to source,
   principal/audience, filters and schema. Avoid global private sequence leakage.
4. **Minimize:** no task inputs, acceptance text, evidence, secrets, credential
   URLs, private actor relationships or low-entropy private-data hashes in public
   announcements. Signatures prove attribution, not confidentiality. Already
   disclosed public information cannot be recalled; visibility changes need
   explicit future tombstone/retention behavior and revocation tests.
5. **Routing is not eligibility:** capability filters help reduce traffic;
   current access, execution eligibility, deadline and claim availability must
   still be checked by the API. Public task fetches remain anonymous-compatible;
   private fetches require signed authorized access. Claims remain signed and
   idempotent. Do not make a notification into a reservation or allocation.
6. **Untrusted input:** bound task metadata and subscription filters; reject
   arbitrary routing expressions/regex/code. Limit subscriptions, topic keys,
   payload sizes, traffic and connect/reconnect rates. Local filters are client
   code, not programs uploaded to the exchange.
7. **Source trust:** use a configured API origin and approved publisher/service
   identity. Never automatically fetch an arbitrary URL from an announcement.
   Validate identifiers and isolate staging/production sources. Integrity/key
   rotation rules for the new feed need a reviewed contract before publication.

## 4. Delivery, bootstrap and recovery contract

Transport independence requires explicit common semantics, not just identical
method names across polling, SSE, WebSocket and broker adapters.

- SQL marketplace state remains authoritative. Discovery is eventually
  consistent and advisory. A stale task announcement may validly lead to a
  rejected claim. A missed notification must not strand marketplace state.
- Use the existing transactional-outbox principle; do not dual-write SQL and a
  remote broker inside a task request and assume atomic success.
- A projection/relay may retry. Choose at-least-once delivery within a specified
  retained window; clients deduplicate using stable source + announcement ID.
  Handle the same task arriving through several topics. No exactly-once promise.
- Define a per-task monotonic revision or equivalent ordering mechanism for
  discovery-relevant transitions. Ignore stale revisions and fetch current
  state on ambiguity. Do not promise total global task ordering.
- Bootstrap requires a bounded, complete, paginated snapshot with a consistent
  replay handoff. Specify a watermark/log-position protocol and prove it cannot
  skip a committed change at the snapshot/live boundary under concurrency.
  UUIDs, wall-clock timestamps or merely allocating a database sequence before
  commit do not establish safe commit-order replay.
- Resume uses an opaque cursor from the discovery service, not the audit cursor,
  task timestamp or envelope signature. Checkpoint only after the client has
  accepted/processed an announcement. Document restart duplicate behavior.
- Expired retention, changed filters/audience, invalid cursors or incompatible
  versions produce an explicit reset/resnapshot requirement, never a silent
  empty stream or false claim of complete replay. Preserve current audit cursor
  behavior; this is a separate feed contract.
- Bound per-connection queues and relay batches. Slow consumers must be paused,
  disconnected with resumable state, or explicitly required to resnapshot—not
  buffered without limit. Detect gaps; do not silently discard lifecycle changes.
- Reconnect with bounded exponential backoff and jitter. Polling fallback must
  be paced, filtered and bounded, never one catalogue request per missed event
  or an immediate synchronized fallback by every disconnected client.
- ACK means neither claim nor task completion. Do not create a generic public
  ACK endpoint just because a broker supports ACKs. A future relay may ACK a
  durable broker write; subscriber checkpointing is separate. Only design
  server-side subscriber ACK state if a real delivery contract needs it.

### Preserve outbox semantics when adding destinations

Current outbox rows have one delivery lifecycle. Running two drainers with
different destinations against that lifecycle can let one destination mark a
row delivered before the other sees it. A future bridge needs reviewed routing
and per-destination delivery tracking, or one durable relay with explicit
fan-out responsibility. It must not create 100k delivery rows per task in the
marketplace database. Preserve event IDs, claim locking, retries and independent
expiry processing. Broker acceptance is not delivery to every agent.

## 5. Proposed SDK abstraction — not implemented

Keep `client.events(cursor=...)` and its future
`client.resources.events.list(...)` wrapper as **audit polling**. Give discovery
its own additive namespace to avoid changing their meaning:

```python
# Interface sketch only: these methods do not exist in today's SDK.
stream = client.resources.discovery.watch(
    filters=interests,
    cursor=saved_discovery_cursor,
    transport=chosen_supported_transport,
)
```

The consumer flow is announcement -> local filter -> current task fetch ->
ordinary claim. Consumer budgeting, scheduling and multi-agent orchestration
remain external. Closing a stream must release resources; cancellation and
shutdown behavior belong in SDK tests.

Before implementing this sketch, define:

- announcement and reset/control message types, filters, cursor/checkpoint
  ownership, error behavior and supported schema versions;
- transport capability negotiation/configuration: durable replay, snapshot
  support, ordering scope, maximum lag/retention and authorization mechanism;
- opt-in fallback behavior when guarantees differ; never silently downgrade a
  durable subscription to best-effort snapshot polling;
- connect authentication and expiry/re-authentication. Current one-request DID
  signatures are not automatically indefinite stream sessions. Native browser
  EventSource cannot simply send arbitrary signed headers; do not put reusable
  credentials in query strings to bypass that limitation.

A future polling discovery adapter may use a new paginated discovery contract.
Today's latest-500 task listing can only offer best-effort snapshots; it cannot
honestly emulate lossless events, invent change IDs or reuse audit cursors.
SSE is a candidate for one-way announcements; WebSocket only if bidirectional
needs justify it; broker clients/gateways are options for routing/fan-out. None
is selected or installed now. The existing `/capabilities` endpoint reports
agent capability counts, **not transport negotiation**; do not repurpose it.

## 6. Compatibility and versioning impact

| Surface | Future change boundary |
|---|---|
| Existing HTTP `/api/v1/tasks` and claims/proofs | Keep request/response semantics for current clients. Better SQL filtering/indices and compatible pagination can be additive, but document changed listing behavior. Never silently redefine a claim as an event ACK. |
| Existing HTTP `/api/v1/events` | Preserve signed actor-scoped audit reads, response shape and cursor interpretation. Do not turn it into a broadcast feed. |
| New discovery HTTP/stream endpoints | Add only after defining routes, authentication, bootstrap/replay/reset/errors and filters. Update OpenAPI for HTTP operations; document framing and message schemas separately for streaming/broker transports. No route is invented by this plan. |
| Discovery message schema | Prefer a separate versioned projection contract. Keep its version separate from HTTP API, SDK and signed-outbox versions; support explicit negotiation/migration. |
| Existing signed envelopes v1/v2 | Preserve their schema, signatures and legacy verification. Capability arrays are not supported by the current scalar attribute model. Do not add routing fields or change meanings while claiming unchanged compatibility. Any extension needs reviewed schema/signature/consumer evolution. |
| Public task revision/snapshot contract | Internal `state_version` is neither exposed nor comprehensive today. Consistently maintain ordering across relevant transitions and specify semantics before exposing it. Preserve `task.version`; new revision data may be additive, not an arbitrary reinterpretation. |
| Python SDK | Add discovery types/transport modules and optional dependencies without breaking flat calls, exception base, imports or audit resources. No mandatory broker dependencies. TypeScript can later implement the same documented contract. |
| Storage | Projection log, indices, retention, audience data and delivery tracking may require additive Alembic migrations with recovery/upgrade/downgrade tests. Keep ORM types out of the SDK. |
| Breaking business/signing semantics | Require an explicit version/compatibility decision. New transport alone need not change task/claim/proof semantics; significant business changes cannot be hidden by an SDK wrapper. |

## 7. Capacity model before picking infrastructure

The 100k target is a design horizon. Before a load test or purchase, agree on:
registered identities, simultaneously active clients/connections, task events/s,
average and hottest-topic subscribers, payload bytes, task fetches/s, competing
claims/task, heartbeat rate, burst/reconnect rate, retention duration, and
p95/p99 latency plus delivery-lag/availability/cost objectives. These values are
**not measured or committed SLOs today**.

Illustrative arithmetic, not a benchmark:

- 100,000 clients polling every 5 seconds -> **20,000 catalogue requests/s**.
- 20 events/s broadcast to all 100,000 -> **2,000,000 deliveries/s**.
- The same 20 events/s with 500 matching recipients/event -> **10,000 deliveries/s**.
- At 600 payload bytes/delivery, that last case is **6 MB/s payload-only**;
  all-client broadcast is **1.2 GB/s**. TLS, framing, retries, replication,
  persistence and operational overhead are additional.

Broker ingress and fan-out egress are different loads. Topics reduce traffic
only if they are selective. Polling versus events must be evaluated against
actual workload and cost, not agent count alone.

### Measurement and acceptance gates

1. **Security gate:** independently review the marketplace controls; test public/private projection, topic
   authorization, replay after revocation, malicious filters and cursor leakage.
2. **Baseline gate:** instrument current API/SQL/worker with synthetic isolated
   data. Measure latency, DB scans/pool/lock waits, claim conflicts, reaper work,
   outbox age, backlog and resource usage. No 100k claim based on unit tests.
3. **Discovery prototype gate:** after separate approval, prove complete bounded
   snapshots, commit-safe replay, duplicates/out-of-order handling, stale claims,
   crash recovery, expired cursors, slow consumers and broker/relay outage.
4. **Scale gates:** progressive tests (for example 1k, 10k, then target active
   subscribers) plus hot topics, high contention, long soak, reconnect storms,
   auth renewal and bounded memory/storage. These are proposed tests, not results.
5. **Launch gate:** compare measured latency/lag/failure/egress/cost to agreed
   SLOs; test backup/restore and rollback. Publish what was actually demonstrated.
   Capacity or public launch needs independent review and a human decision.

Fan-out does not solve transactional bottlenecks. Profile query pagination,
indices, connection pooling, registration writes, request-time bulk reaping,
heartbeats, claims, nonce/idempotency retention and hot-row contention. Future
read-path optimization may decouple bulk maintenance from catalogue requests,
but must preserve direct fail-closed lease/deadline checks on work mutations.
Do not remove correctness guards just to improve a load-test number.

## 8. Deployment choice and staged delivery

**Discovery runtime:** still design-only; keep the API private and complete the
security/accounting review gate first. No Redis, Kafka,
NATS, SSE, WebSocket or automatic subscription fallback introduced for a diagram.

**Protected staging:** Vercel static website/docs if useful; API and worker on a
VPS or suitable persistent container service; private PostgreSQL. A small
single-host Compose setup is an initial experiment, not a sizing recommendation
or a high-availability/100k solution. PostgreSQL on the same host shares its
failure domain. Managed PostgreSQL can reduce operational work but still needs
secure networking, migrations, backup restoration tests and cost/latency review.
No GPU is required by the current mock inference implementation.

**Measured growth:** add a discovery projection/replay service and selected
routing transport only when tests/usage justify them; later isolate gateways
and API replicas behind appropriate ingress if required. Do not allocate a DB
connection per live subscriber. Keep finite pools and bounded worker batches.
Migrations run in a controlled release step, not independently on every replica.
The continuous reaper remains necessary even if publishing is disabled.

**Domain:** stable configured origins help clients survive hosting changes, but
no quoted hostname is reserved or live. A domain is optional before staging;
buying/reserving a desired name is a business choice, not prohibited until audit.
An optional docs subdomain is not needed if one static site suffices. Migrating
hosts must preserve signing paths, TLS/trust configuration and client identity,
not merely DNS. The current website has links, not browser API JavaScript.

## 9. Later implementation file map and handoff

| Area | Files likely affected after approval |
|---|---|
| Transition coverage / public projection | Focused services and relevant `app.py` transitions; new projection module and tests; do not embed transport code in task business logic |
| Durable relay and per-destination progress | `outbox.py`, `worker.py`, adapters; possibly `models.py` and Alembic; preserve signed-outbox regressions |
| Discovery queries and audience enforcement | New router/service and selected current list queries; authorization policy; indices/access models only as needed |
| Wire contracts | New reviewed schema(s)/stream specification, OpenAPI, protocol docs and package resource/parity tests; retain existing contracts |
| SDK | Add `resources/discovery.py` and transport modules only at the SDK implementation stage, packaging metadata and standalone tests; preserve old audit methods |
| Operations | Deployment settings, metrics, synthetic load/failure tests and runbooks after topology is chosen; no vendor manifests now |

For current work and handoff, use [PROJECT_STATUS.md](PROJECT_STATUS.md) and
[GITHUB_HANDOFF.md](GITHUB_HANDOFF.md). This document preserves the accepted
discovery design, not its implementation status. Ordinary polling remains;
no discovery interface, broker or 100k concurrency claim is implied.
