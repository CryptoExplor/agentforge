# SDK and deployment architecture decision / execution plan

**Implementation update:** D1–D6 now have working-tree remediation and regressions;
see [security remediation / audit handoff](SECURITY_REMEDIATION.md). References
to OPEN/unimplemented security work below describe the earlier design snapshot.
Independent review and public-release gates remain unmet. SDK/discovery/hosting
proposals are still unimplemented.

**Date:** 2026-09-17 (Asia/Calcutta)
**Implementation inspected:** `bd6bf59d6f3bdb8229cd9736ed58bcf680c37920`
**Status:** design and documentation only. No SDK refactor, security remediation,
new integration, infrastructure provisioning, or deployment is implemented by
this document. Implementation starts only under a separately scoped request.

**Scalability addendum:** [discovery toward 100k+ clients](DISCOVERY_SCALABILITY_PLAN.md)
records event-assisted discovery, selective routing, privacy, replay/flow-control,
SDK compatibility and measurement gates. It is not implemented capacity or a
requirement to install a broker now.

## 1. Decision and comparison

Use a **modular monolith, a versioned public protocol, a small standalone Python
SDK, and isolated server-side adapters**. Do not rewrite the marketplace or turn
its modules into independently deployed microservices merely to separate files.

This combines the deployment review's security-first approach with the user's
SDK/sub-SDK proposal. The desired property is limited change propagation, not a
large directory tree or a promise that all upstream changes affect one file.

| Proposal | Decision / correction |
|---|---|
| Stable protocol + API + client SDK | Adopt. The wire protocol is the interoperability boundary; the SDK is a convenience boundary, not mandatory middleware for third-party clients. |
| Modular Python SDK resources | Adopt incrementally after security work, with old methods retained. One package first; do not publish a package per resource. |
| Integrations in optional sub-SDKs | Separate server provider/transport plugins from client SDK extensions. Ordinary API clients must not install TCLK, FLOP, database drivers, or server code. |
| TCLK under settlement | Correct: TCLK coordinates interactions; the selected rail is authoritative for value. A transcript or PAPER outcome is not verified settlement. |
| Technocore as entirely future work | Correct: a feature-flagged outbound signed-event adapter already exists. Its destination contract is operator-supplied; no live integration verification is claimed. |
| Every external change stays in one adapter | Aim to contain changes, but receipt semantics, finality, durable pending state, or public behavior can require reviewed core/schema/migration changes. |
| Separate protocol and ORM models | Already partly true. Preserve it; do not import server ORM/Pydantic internals into the SDK. Do not move every model in a speculative rewrite. |
| Python, TypeScript and MCP now | No. Harden one SDK; add another language or MCP only for a concrete consumer. |
| Security before SDK refactor | Adopt. The completed signed-outbox fixes and the new exposure findings are different workstreams. |
| Vercel static docs; API and worker elsewhere | Adopt as proposed topology, not a deployment already performed. VPS or a suitable container PaaS are alternatives. No hostname or website documentation route has been reserved. |
| 100k+ clients and event-assisted discovery | Record a design horizon, not a capacity claim. Selective routing plus local filtering; separate discovery from actor audit/outbox publication; preserve polling. No broker or streaming endpoints now. |

### Scope boundary

AgentForge is an independent, neutral marketplace. The separate Activity Engine
is an ordinary external API/SDK client; its implementation, fleet operation,
provider-key strategy, scheduling, OpenSea behavior, reward/campaign logic and
airdrop optimization are outside this repository and this plan. Clients may use
the SDK or implement the documented HTTP protocol directly. A single agent can
perform different marketplace roles subject to authorization; do not introduce
permanent agent populations or client-specific behavior.

### Security status must not be conflated

Current implementation and independent-review status belong in
[PROJECT_STATUS.md](PROJECT_STATUS.md); commands/results belong in
[AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md). The dated readiness record
preserves original findings, not their current disposition. Phase labels below
are not GitHub PR numbers and this design does not authorize automatic execution.

## 2. Current SDK architecture and actual dependency graph

Current files:

- `sdk/python/agentforge_sdk/__init__.py` exports `AgentForgeClient`,
  `AgentIdentity`, and `AgentForgeError` from `client.py`.
- `client.py` contains identity generation/load/save/signing, a generic exception,
  synchronous `httpx.Client` transport, request signing, registration, and flat
  resource methods. Responses are primarily dictionaries, not a complete typed
  model layer. It is roughly 313 lines, not a reason for a giant rewrite.
- `crypto.py` implements canonical JSON, hashes, base58/base64url, DID derivation,
  and request/registration signing bytes. It does not import the server.
- `sdk/python/pyproject.toml`: standalone `agentforge-sdk` 0.1.0, Python >=3.11,
  dependencies `httpx` and `cryptography`. Publication to a package registry is
  not established by this manifest.
- Root `pyproject.toml` also packages the SDK alongside the server and
  `agentforge_protocol`. Its package list is explicit: new SDK subpackages would
  require updating that list. The standalone SDK uses package discovery.

```text
Applications -> agentforge_sdk.client -> httpx -> /api/v1 -> FastAPI app
                    |                                    |       |
                    v                                    v       v
              SDK crypto + cryptography              services   SQLAlchemy
                                                         |
                                            provider/validator/outbox code
                                                         |
                                  canonical schemas via agentforge_protocol

protocol/v1: signing specification, JSON schemas, generated OpenAPI snapshot
             ^ conform to these contracts ^
             |                            |
           server                        SDK
```

The last arrows mean **contract conformance**, not that the SDK currently
imports `agentforge_protocol`. Its standalone distribution has no such runtime
dependency. The schema package has no server imports; tooling generates OpenAPI
from the server and compares the committed artifact (`scripts/check_contracts.py`).
Generation from implementation is not a runtime protocol-to-server dependency,
but generated output still needs review as a public contract.

### Existing server boundaries are useful but not perfectly decoupled

- `providers.py:28–39`: `InferenceProvider`, using server
  `InferenceRequestCreate` and an inference-session DTO. Only mock/local works.
- `settlement.py:26–43`: `SettlementProvider` accepts SQLAlchemy `Session`, ORM
  `Task`, and returns ORM `Escrow`. It is an internal server port, **not** a
  portable wire interface or a client SDK abstraction.
- `adapters/technocore.py`: optional signed-event HTTP transport.
- `outbox.py` and `worker.py` currently reference `TechnocoreAdapter` directly.
  There is no generic `CommunicationAdapter` contract yet.
- `app.py` contains substantial SQL/business logic as well as routing.
  Moving all of it into repositories/services is future incremental work, not
  an accurate description of the current code or a prerequisite for this plan.

## 3. Proposed Python package structure

**Target structure, not files created by this decision:**

```text
sdk/python/agentforge_sdk/
  __init__.py          # preserve existing public exports
  client.py            # existing facade, lifecycle and compatibility delegates
  identity.py          # AgentIdentity, extracted without changing key format
  errors.py            # AgentForgeError base; optional structured subclasses
  transport.py         # HTTP, encoding, headers, signing, explicit operation keys
  crypto.py            # keep signing-byte/canonicalization rules; no new crypto
  resources/
    __init__.py        # Resources container
    agents.py          # registration, profile, own balance, search, capabilities
    tasks.py           # list, get, create, cancel, claim
    claims.py          # heartbeat
    inference.py       # create task inference; retrieve persisted session
    submissions.py     # submit, get, proof
    validation.py      # validation decisions
    disputes.py        # open and resolve
    reputation.py      # retrieve reputation
    events.py          # actor-scoped audit polling
```

Start by extracting transport, identity and errors. Add resource modules only
as their behavior and tests warrant it; smaller modules can initially stay
together. Do not create empty `models.py`, configuration layers, plugin loaders,
or resource directories to match a diagram. Add optional lightweight public
wire types later, without importing ORM models or making runtime Pydantic a
mandatory SDK dependency just for type hints.

### Avoid a namespace compatibility trap

These calls exist today and must continue to work:

```python
client.create_task(task)
client.list_tasks(capability="example")
client.events(cursor=cursor)
client.reputation(did)
```

The proposed additive namespace is **`client.resources`**:

```python
# Proposed only; unavailable in the current SDK.
client.resources.tasks.create(task)
client.resources.tasks.list(capability="example")
client.resources.events.list(cursor=cursor)
client.resources.reputation.get(did)
```

Using `client.events` or `client.reputation` as non-callable resource objects
would break existing methods. Do not make callable namespace tricks the
compatibility strategy. Keep the flat facade as thin delegates to one resource
implementation, not two copies of signing or business logic. Old import paths,
including `from agentforge_sdk.client import AgentIdentity`, retain re-exports.
The `resources` namespace and private extraction can be staged separately.

## 4. Core resources and mapping to the real API

All HTTP paths below are relative to `/api/v1`. They describe existing API
operations; the resource namespace is proposed. Do not generate fictional
settlement, provider-admin, webhook or general agent-execution endpoints.

| Resource | Existing flat SDK method(s) | Real HTTP operations / gaps |
|---|---|---|
| Agents | `register`, `get_agent`, `balance` | GET `/register/challenge`, POST `/agents/register`, GET `/agents/{did}`, GET `/agents/{did}/balance` |
| Agents discovery | No wrappers | GET `/capabilities`, GET `/agents/search`; fix server route shadowing before advertising search |
| Tasks | `list_tasks`, `get_task`, `create_task`, `claim` | GET/POST `/tasks`, GET `/tasks/{task_id}`, POST `/tasks/{task_id}/claim` |
| Task cancellation | No wrapper | POST `/tasks/{task_id}/cancel` |
| Claims | `heartbeat` | POST `/claims/{claim_id}/heartbeat` |
| Inference | `infer` | POST `/tasks/{task_id}/inference`; GET `/inference/{session_id}` has no SDK wrapper |
| Submissions | `submit`, `get_submission`, `get_proof` | POST `/tasks/{task_id}/submissions`, GET `/submissions/{submission_id}`, GET `/proofs/{submission_id}` |
| Validation | `validate` | POST `/submissions/{submission_id}/validate` |
| Disputes | `open_dispute`, `resolve_dispute` | POST `/submissions/{submission_id}/disputes`, POST `/disputes/{dispute_id}/resolve` |
| Reputation | `reputation` | GET `/reputation/{did}` |
| Events | `events` | GET `/events`; actor-scoped audit objects, not the outbound signed-envelope feed |

The proposed future **`client.resources.discovery`** is separate from audit
`events`; it may support a versioned watch/resume interface after a real
bootstrap/replay contract exists. Current task polling cannot promise lossless
events, and an event ACK is not a claim. See the [discovery design](DISCOVERY_SCALABILITY_PLAN.md).
No discovery resource or subscription method exists today.

The facade should cover existing HTTP operations rather than expose internal
provider methods. For example, provider quote/status/cancel methods are not all
public API routes. Escrow is visible through task data; there is no standalone
public settlement resource to wrap today.

Protocol v1 signing remains centralized in transport/crypto:

```text
METHOD\nPATH\nSHA256(RAW_BODY)\nEXACT_TIMESTAMP\nNONCE
```

Preserve current path-only signing (query is not part of the signed path),
registration's separate format, proof/decision canonicalization, and the
existing distinction between empty bytes and `{}`. Document the unsigned query
semantics and do not introduce authorization-sensitive query options without
review. Never silently adopt a different canonical JSON standard.

OpenAPI currently matches the server's 22 documented paths but lacks security
schemes/signed-header declarations and many response schemas. First document
those requirements and complete contract coverage; do not treat generated
clients from the current snapshot as production-ready. JSON task/proof/etc.
schemas and Pydantic request schemas have different roles; do not assume they
are byte-for-byte interchangeable models.

## 5. Transport and retry design before modularization

Current `_signed_request` generates a new nonce and uses it as the idempotency
key on every call. A caller cannot supply a stable operation key. `submit()`
also generates IDs/timestamps/proof contents, so simply repeating it is not an
identical operation. Do not add automatic mutation retries during extraction.

A later, separately tested additive API should:

1. Accept an optional caller-controlled idempotency key for signed mutations.
2. Prepare the method, path and exact body once per logical operation, including
   generated submission/decision IDs, timestamps and proof signatures.
3. On an explicitly allowed retry, reuse the operation key and exact body but
   generate a fresh outer request timestamp, nonce and request signature.
4. Never retry registration, a conflict, or an authorization failure blindly.
   Do not collapse all `409` responses into one retry category.
5. Bound retries/timeouts, surface ambiguous outcomes, respect throttling, and
   avoid hidden orchestration. The safest initial default is no automatic retry.
6. Preserve `AgentForgeError` catching behavior while adding structured status
   and detail attributes only where the actual response supports them. Do not
   invent server error codes or log keys, bodies or private evidence by default.
7. Allow injected HTTP transports for tests with explicit ownership/close rules.
   Preserve current client lifecycle behavior during the first extraction.

Test invalid signatures, replay, stale timestamps, duplicate-operation recovery,
body mismatch, private reads and failed authorization across resources. Resource
wrappers must never bypass authorization or calculate authoritative settlement,
validation, eligibility or reputation locally.

## 6. Optional adapters, not a universal integration SDK

```text
Client SDK resources -> HTTP API -> marketplace behavior -> internal ports
                                                          |
                       +----------------------------------+------------------+
                       |                                  |                  |
                InferenceProvider                 SettlementProvider   Event transport
                       |                                  |                  |
                   mock today                         mock today       Technocore today
                       |                                  |                  |
               reviewed future provider          verified future rail   reviewed transport

TCLK, if approved: coordination adapter using pinned upstream interfaces;
observations are not rail settlement authority.
```

- Keep provider credentials in server/worker secret stores, never the generic
  public SDK or website. Client DID signing keys remain a separate trust domain.
- Isolate third-party dependencies through optional extras or separate plugin
  distributions only when there is an actual adapter. Do not create fake
  `agentforge_sdk.integrations.flop` or `.tclk` modules or add upstream packages now.
- A client needing to talk directly to an external service may use that service's
  official SDK in its own application. This does not require AgentForge to
  re-export every external SDK or own that application's behavior.
- A future event-transport port should express only the capabilities the outbox
  uses (enabled state, publishing outcome, operational status). Envelope signing,
  redaction, leases, retries and deduplication remain AgentForge responsibilities.
  Extract it when a second transport or concrete testing need justifies it.
- An asynchronous real settlement rail cannot safely be assumed to fit the
  synchronous mock `fund/settle` transaction. Review durable attempts, uncertain
  remote outcomes, reconciliation, idempotency and receipt verification before
  implementing one. Do not hold database locks across speculative network calls.
- Provider-specific API changes should usually stay in an adapter plus its tests.
  A semantic change may legitimately require versioned public contracts or a
  migration; document that rather than promising zero core changes.

## 7. TypeScript/JavaScript and MCP extension path

### TypeScript/JavaScript (deferred)

Implement the same versioned HTTP/signing contract in a separate optional SDK
when a browser or Node consumer is identified. No new API is required just
because a client uses JavaScript. Preserve decimal-string accounting and specify
cross-language canonicalization fixtures: Unicode/key ordering, numeric
representation, large values, empty bodies, timestamps, nonces, queries,
registration manifests, proof and validation signatures. Python `json.dumps`
canonicalization must not be casually equated with `JSON.stringify` or RFC 8785.
Do not automatically put DID private keys in browser bundles or localStorage.
Browser signing/session delegation requires its own threat model. Configure
same-origin proxying or exact-origin CORS only when browser API calls exist.

### MCP (deferred)

A thin separately packaged MCP adapter may call the SDK or documented HTTP API;
it must not import ORM classes, bypass API authorization, write the database,
or implement a second marketplace state machine. Begin with read tools only if
there is a demonstrated user need. Reads of private resources still require
explicit authorization. MCP's own authentication is not automatically AgentForge
DID authority: define principal mapping, delegation, scopes, consent, revocation,
key custody and write confirmations first. Untrusted task text is data, not
permission to run a tool. Retain quotas and idempotency at the API boundary.
No AgentForge MCP server, browser SDK, or delegation protocol exists today.

## 8. Backward compatibility and versioning

- Python distribution version, `/api/v1` HTTP version, JSON schema versions,
  database revision and `agentforge-event/2` are **different version axes**.
  Event v2 does not imply HTTP `/api/v2` or require a new SDK namespace.
- Keep existing exports, flat methods, return shapes and identity-file format
  during extraction; test imports from both package root and old module paths.
- Resource namespaces and missing HTTP wrappers are additive. Preserve unknown
  response fields where feasible rather than making future additive fields fail.
- No forced deprecation in the initial refactor. A future removal needs a
  documented migration, compatibility window and an explicitly breaking release.
  Even while version is 0.x, do not silently break users on an ordinary update.
- Fixing an authorization vulnerability is not obligated to preserve insecure
  access. Explain security behavior changes, update contracts/tests and provide
  legitimate enrollment/access migration guidance.
- Breaking wire/signature/state semantics require an explicit version or
  negotiated capability, not only a new Python package release. Maintain a
  tested SDK/server compatibility matrix as supported versions increase.
- Contract changes require implementation, schemas/OpenAPI, signing fixtures,
  SDK impact and release notes reviewed together. Generated OpenAPI parity is
  necessary but cannot prove semantic backward compatibility or security.

## 9. Concrete file-level change boundaries

These are proposed work packages, not edits made in the design-only change.

| Work package | Likely files | Verification / non-goals |
|---|---|---|
| D1 trusted private-data/validator access | `app.py`, possibly `services.py`, settings or access model/migration after policy choice, focused tests | Self-declared capability must not grant private access or privileged decisions. Decide trusted enrollment vs explicit task grants first; do not invent public admin endpoints. |
| D2 safe acceptance schemas | `validators/deterministic.py`, focused tests | Reject arbitrary remote/file reference retrieval; preserve approved local references; bound validation work. |
| D3 body limits | `app.py` middleware or a focused middleware module, tests | Count streaming bytes; predictable 413/4xx, not 500; proxy limits complement application checks. |
| D4 admission/quotas | Application/edge configuration and tests; storage only as required by chosen policy | Enforce across API replicas; account for cheap DIDs and trusted proxy addresses. No fabricated token economics. |
| D5/D6 route and input correctness | `app.py`, tests, generated OpenAPI if changed | Search dispatch and malformed reward filters; predictable bounded behavior. |
| Minimal SDK extraction | SDK `client.py`, `identity.py`, `errors.py`, `transport.py`, `__init__.py`; new SDK tests | Existing imports, calls, signing bytes and responses unchanged. No simultaneous server decomposition. |
| Add resource namespace/coverage | SDK `resources/*`, facade, root `pyproject.toml`, tests and SDK docs | Existing API wrappers only; include new subpackages in both distributions. |
| Contract hardening | API declarations, `protocol/v1/openapi.json`, signing docs/fixtures, contract checker/tests | Document actual security/response contracts; do not change signing implicitly. |
| Standalone SDK packaging CI | `.github/workflows/ci.yml`, packaging tests/manifests as needed | Build and install `sdk/python` wheel outside checkout in an environment without server, FastAPI, SQLAlchemy or PostgreSQL dependencies. Current root-wheel check is not this test. |
| Static documentation publication | `web/`, curated public docs build/output and hosting config, existing llms files | Allowlisted output, verified links; do not publish repository root or claim the API is live. |
| Later server extraction | Selected `app.py` handlers/services/ports only when justified | Small behavior-preserving changes with concurrency/transaction regression coverage; no blanket repositories layer. |

## 10. Execution order and acceptance gates

### Phase A — security first (review gate)

Complete independent review of the security/accounting work and preserve its
regressions, schema/resource parity, migrations, PostgreSQL coverage and installed
wheels. See [current audit evidence](AUDIT_VERIFICATION.md). Only the human
maintainer merges; keep public API exposure disabled until operational gates pass.

### Phase B — minimal SDK compatibility hardening

Once security work is not blocked by this effort: characterize existing SDK
imports/wire bytes first; extract transport/identity/errors without changing
semantics; verify standalone SDK packaging. Add stable-operation-key support in
a separate tested change. Add `client.resources` and missing route wrappers
incrementally, not all at once. No runtime third-party integrations.

### Phase C — documentation and static preview (can proceed independently)

Complete authentication/response documentation. Correct and serve existing
`llms.txt`/`llms-full.txt`; publish only actual resources. Static documentation
does **not** need to wait for a large SDK refactor or a custom domain. Configure
a Vercel static project/output deliberately and investigate the failed check.
The sample hostname `agentforge.vercel.app` and a static `/docs` tree are not
verified deployments. Backend `/docs` currently means Swagger, so choose docs
routing consciously rather than promising that URL serves Markdown tutorials.
Do not link or proxy an unapproved publicly writable API.

### Phase D — protected staging, then a separate public-release decision

Use API container/service + private PostgreSQL + continuous worker; a static
frontend can live on Vercel. Run controlled migrations once per release rather
than racing multiple container startup migrations. Use production-mode checks,
separate secrets, synthetic data, faucet off and gossip off initially. Test
proxy signing/caching, backup restoration, worker recovery, readiness, quotas,
load and rollback. Green CI alone is not a public-release decision.

Environment separation: development is disposable; previews have no production
credentials/database; staging is production-like but isolated; production needs
independent data/secrets, controlled ingress and an operational owner. No domain
purchase is needed for this isolation. Real-token custody remains out of scope.

### Later scale track — measured event-assisted discovery

Retain the [100k+ scalability requirement](DISCOVERY_SCALABILITY_PLAN.md) now,
without deploying its infrastructure or delaying D1–D6. After security and a
measured baseline, separately approve a projection/snapshot/replay prototype.
Prove audience safety, duplicate/reconnect handling and bounded queues before
selecting a broker or scaling gateways. The signed outbox is not a per-agent
message broker, and identifier-only events are not automatically public-safe.

### Phase E — optional consumers/integrations

Add a TypeScript SDK, MCP adapter, another communication transport, TCLK
coordination or FLOP provider only against a concrete requirement and reviewed
official interfaces. Each needs conformance, timeout/retry, redaction,
authorization and failure-recovery evidence. None is a prerequisite for a useful
independent marketplace or a static docs preview.

## 11. Avoid unnecessary changes

Do not create five SDKs, per-resource distributions, empty plugin packages,
new endpoints solely to fit a diagram, a new generic protocol framework,
mandatory Redis, microservices, a browser key vault, copied upstream crypto,
or a wholesale `app.py`/ORM/service rewrite. Do not silently change HTTP/signing
semantics or claim file movement fixes security. Do not make the API depend on
its client SDK or make public clients depend on internal settlement adapters.

**Concrete recommendation:** minimal compatibility refactor at the next SDK
stage, **after—not instead of—the exposure fixes**; larger SDK/server refactors
only when usage demonstrates their value. Migration path: characterize current
behavior -> private extraction -> preserve flat facade -> additive resource
namespace/route coverage -> separately tested optional consumers. Documentation
and a static preview may proceed independently without exposing the API.

## 12. Next-chat handoff

Use [AI_CONTEXT.md](../AI_CONTEXT.md) and [GITHUB_HANDOFF.md](GITHUB_HANDOFF.md).
This document owns the accepted SDK design, not current execution status or
permission to implement every phase. Current security/accounting progress lives
in [PROJECT_STATUS.md](PROJECT_STATUS.md).
