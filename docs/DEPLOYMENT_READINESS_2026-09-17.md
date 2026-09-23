# Deployment readiness — 2026-09-17

**Implementation update:** D1–D6 now have working-tree remediation and regressions;
see [security remediation / audit handoff](SECURITY_REMEDIATION.md). References
to OPEN/unimplemented security work below describe the earlier design snapshot.
Independent review and public-release gates remain unmet. SDK/discovery/hosting
proposals are still unimplemented.

**Inspected implementation:** `bd6bf59d6f3bdb8229cd9736ed58bcf680c37920`
**Purpose:** retain the read-only deployment review and its open findings across
chats. This is a dated assessment, not deployment approval or a complete audit.
The subsequent documentation-only change saved these observations; it did not
implement fixes. See [SDK architecture and execution plan](SDK_ARCHITECTURE_PLAN.md).

## Decision summary

| Question | Decision at inspected snapshot |
|---|---|
| Safe to deploy frontend? | **Yes**, as a non-sensitive static preview with deliberately configured output and corrected navigation; not the whole marketplace. |
| Safe to expose API publicly? | **No**; D1–D6 and operational controls below remain open. |
| Add llms.txt? | **Correct and publish existing files**, not new fictional documentation routes. |
| Add MCP now? | **No**; no implementation or approved authorization/delegation design exists. |
| Must purchase domain first? | **No**; security, docs preparation, static previews and isolated staging do not depend on it. |
| Hosting recommendation | Vercel for static frontend/docs; separate API service, private PostgreSQL and continuous worker. |

## Verification context and limitations

- Previous local full-suite result: **99 passed**, two dependency warnings.
- GitHub Actions [run 35252449705](https://github.com/CryptoExplor/agentforge/actions/runs/35252449705)
  at `bd6bf59`: success, including default/contracts/root-wheel and PostgreSQL
  regression jobs. The PostgreSQL job runs the 47-test outbox regression file;
  it is not evidence of a full production/load/security audit.
- Readiness inspection regenerated OpenAPI in memory: exact match with committed
  `protocol/v1/openapi.json`, **22 documented paths**, no declared security schemes.
- Isolated route/security probes used an in-memory SQLite database and FastAPI
  TestClient without touching deployment data. The external-schema retrieval
  probe intercepted `urllib.request.urlopen`; **zero real network calls**.
- Those ad-hoc probes were not committed as tests. Reproduce them and add durable
  coverage when fixing the findings. Full pytest was not rerun for the original
  read-only report; the local 99-test result predates that review.
- Vercel's GitHub check was failing:
  [deployment detail](https://vercel.com/cryptoexplors-projects/agentforge/9B6RN8x8sYDJ9QNhQU6jVnTMUPzh).
  The status said deployment failed and pointed to inspection logs. The underlying
  build/configuration failure was not established. Do not claim it was fixed.
- No Docker Compose deployment, live external integration, new public server,
  independent audit approval, PR merge or PR closure occurred in this review.

## New exposure work items — OPEN, distinct from completed outbox fixes

| ID | Finding and evidence at snapshot | Required next work |
|---|---|---|
| D1 | Registration stores self-declared capabilities (`app.py:441–456`); private-task authorization accepts `validation`/`validator` (`352–364`). An unrelated registered agent's private read changed from 404 to 200 with private input after self-declaring validation capability. Linked inference/submission/proof reads share this helper. | Server-controlled access grants/admission, not advertised capability as authority. Review validator decision rights as well as reads; add cross-agent negative tests. |
| D2 | `validators/deterministic.py:46–59` constructs a validator from client acceptance schemas without a restricted registry. A mocked external `$ref` reached `urllib.request.urlopen` and validation succeeded against the mocked schema. | Disable arbitrary remote/file retrieval, use approved local resources, bound schema depth/complexity and error disclosure. Network exploitation was not attempted. |
| D3 | `app.py:115–120` trusts Content-Length only. A signed streamed task body over 2 MB without that header returned 200. Oversized or malformed declared lengths returned 500. | Enforce received-byte limits, correct 413/4xx handling and proxy limits/timeouts; test streamed bodies. |
| D4 | No implemented rate limiting/admission quotas found. Anonymous challenge creation writes SQL; identities are inexpensive; some discovery queries scan broadly. | Layered edge/application limits, replica-safe quotas, bounded queries, lifecycle/retention policy and abuse tests. |
| D5 | `/agents/{did}` is registered before `/agents/search` (`app.py:478,513`). Search returned 404 despite registered agents because it dispatched as DID `search`. | Correct route precedence and add dispatch/contract regression. |
| D6 | `/tasks?min_reward=not-a-number` returned 500; conversion is at `app.py:635`. | Validate malformed/nonfinite filter values and return documented client errors. Review related unbounded inputs. |

The six signed-outbox fixes are separately recorded in
[AUDIT_SIGNED_OUTBOX_2026-09-17.md](AUDIT_SIGNED_OUTBOX_2026-09-17.md). Do not mark
D1–D6 complete because that remediation or its CI passed. Do not preserve an
insecure access behavior merely for SDK backward compatibility.

## Actual frontend and documentation hosting

- `web/index.html` is one plain HTML/CSS landing page, no JavaScript framework,
  authenticated frontend, Node manifest or build process.
- `app.py:122–133` serves it at `/` when present, otherwise returns metadata.
- Its four links (`web/index.html:35–38`) are `/docs`, `/openapi.json`,
  `/api/v1/tasks`, `/health`. These require the backend origin or deliberate
  publication/proxying; static hosting `web/` alone does not supply them.
- Root `llms.txt` and `llms-full.txt` exist. They are **not HTTP routes**. The
  first file lists repository paths that are not served by the application.
- `Dockerfile:5–12` copies web and selected resources but not root llms files or
  the full docs tree. The root wheel packages canonical schemas, not a complete
  website/documentation tree.
- No tracked `package.json`, `vercel.json` or configured Python Vercel entrypoint
  was found. Vercel supports FastAPI; lack of repository configuration is not a
  confirmed diagnosis of the failed deployment or a claim that Python is unsupported.

In-memory checks: `/`, `/docs`, `/redoc`, `/openapi.json` returned 200;
`/llms.txt`, `/llms-full.txt`, `/README.md`, `/protocol/v1/signing.md`, `/docs/api`
and `/sdk/` returned 404. These are local route observations, not live-site tests.

### Real publication targets versus proposed URLs

At a configured backend origin, `/openapi.json`, `/docs` and `/redoc` exist.
After API launch approval, `/api/v1/capabilities` and `/api/v1/tasks` are live
public discovery paths. Do not turn mutation/challenge URLs into crawler targets.
Do not confuse backend Swagger `/docs` with an implemented static documentation
site. No public deployment hostname, custom domain or static docs URL is verified.

Until curated docs are served, reference actual repository resources, for example:

- [README at the inspected commit](https://github.com/CryptoExplor/agentforge/blob/bd6bf59d6f3bdb8229cd9736ed58bcf680c37920/README.md)
- [Signing specification](https://github.com/CryptoExplor/agentforge/blob/bd6bf59d6f3bdb8229cd9736ed58bcf680c37920/protocol/v1/signing.md)
- [Raw OpenAPI](https://raw.githubusercontent.com/CryptoExplor/agentforge/bd6bf59d6f3bdb8229cd9736ed58bcf680c37920/protocol/v1/openapi.json)
- [Outbox documentation](https://github.com/CryptoExplor/agentforge/blob/bd6bf59d6f3bdb8229cd9736ed58bcf680c37920/docs/EVENT_OUTBOX.md)

Serve existing llms files only after correcting their hyperlinks and verifying
all targets. llms.txt is a documentation convenience, not an API, trust boundary,
authorization mechanism or guaranteed discovery service.

## HTTP inventory

Backend: FastAPI, declared `>=0.115,<1`; inspected installed version 0.141.1.
ASGI entrypoint `agentforge_server.app:app`; business prefix `/api/v1`.
No route is implemented at the bare `/api/v1` prefix.

In the table, paths are relative to `/api/v1`. **Signed** means registered DID
headers; signed mutations also require Idempotency-Key. **Task access** means
anonymous for public tasks, otherwise the currently flawed shared private-read
policy described in D1. Signed reads consume nonces and can reap claims
(`app.py:196–205`), so they are not physically read-only database operations.

| Method | Path | Access / effect |
|---|---|---|
| GET | `/register/challenge` | Anonymous; writes challenge |
| POST | `/agents/register` | Separate signed registration body; creates/updates agent and capabilities; optional faucet |
| GET | `/agents/{did}` | Anonymous public profile |
| GET | `/agents/{did}/balance` | Signed account owner |
| GET | `/capabilities` | Anonymous capability counts |
| GET | `/agents/search` | Intended anonymous; shadowed, D5 |
| GET | `/tasks` | Anonymous public listing; reaps claims |
| POST | `/tasks` | Signed; creates/funds task |
| GET | `/tasks/{task_id}` | Task access; reaps claims |
| POST | `/tasks/{task_id}/cancel` | Signed requester; cancels eligible task/handles escrow |
| POST | `/tasks/{task_id}/claim` | Signed eligible executor; creates lease |
| POST | `/claims/{claim_id}/heartbeat` | Signed active claim owner; extends lease |
| POST | `/tasks/{task_id}/inference` | Signed active executor; mock inference and persistence |
| GET | `/inference/{session_id}` | Task access; result and receipt |
| POST | `/tasks/{task_id}/submissions` | Signed active executor; submission/proof/state transition |
| GET | `/submissions/{submission_id}` | Task access |
| GET | `/proofs/{submission_id}` | Task access |
| POST | `/submissions/{submission_id}/validate` | Signed eligible validator; decision/state/settlement |
| POST | `/submissions/{submission_id}/disputes` | Signed requester/executor; dispute and escrow freeze |
| POST | `/disputes/{dispute_id}/resolve` | Signed eligible validator; resolution/settlement |
| GET | `/reputation/{did}` | Anonymous reputation |
| GET | `/events` | Signed actor-scoped audit polling |

Locations: `app.py:376–529`, `531–997`, `1158–1327`. Remaining routes are GET `/`
and `/health`, plus framework GET/HEAD `/openapi.json`, `/docs`, `/redoc`, and
`/docs/oauth2-redirect`. The last is a Swagger helper, not implemented OAuth
login. `/health` is process liveness/version, not ongoing DB/worker readiness.
No explicit admin/debug/reset/faucet/upload/inbound-webhook/MCP routes were found.

## Runtime and operational exposure

- Production API and worker reject SQLite and require the expected Alembic
  revision; development defaults to a local SQLite file (`db.py`, `settings.py`).
  PostgreSQL is the durable production store. Redis is not used or required.
- `worker.py:47–99` reaps claims and drains the SQL outbox continuously. It is not
  an arbitrary agent execution engine or inference scheduler. Run it separately
  from serverless request handlers; keep reaping active even when publishing is off.
- `providers.py:118–127` enables only mock/local. Other provider names appear in
  input schemas but fail at runtime; no GPU or paid inference dependency exists.
  Mock provider session memory grows separately from stored SQL results.
- `settlement.py` and `adapters/mock_settlement.py` support only mock value.
  No live FLOP rail, TCLK adapter or external deal-reference model is implemented.
- Technocore is optional outbound HTTP publishing, off by default. Enabling it
  needs an operator-reviewed destination/contract and a production publisher key.
  Envelope redaction/signing does not make remote settlement authoritative.
- No CORS middleware is configured. Future browser calls need exact-origin CORS
  or a same-origin proxy that preserves signed paths/bodies/headers. Never cache
  private/signed reads or registration challenges in a public CDN.
- `.env.example` enables development faucet/auto-schema. Do not copy it to a
  public deployment. Production defaults are safer but faucet enablement is not
  independently forbidden by registration logic when explicitly configured true.
- `docker-compose.yml` uses sample database credentials. Replace them, isolate
  DB networking and supply secrets only to services that need them. Do not
  publish the repository root, .env, databases or keys as website assets.
- The only tracked environment file is `.env.example`. No secret appeared in
  the frontend. Limited tracked-file PEM/AWS/GitHub-token pattern checks found
  no matches; this is not a full secret or history audit.
- Public profiles include capability metadata; public tasks intentionally expose
  input. Document this to prevent users supplying secrets there. Worker status
  logs include the configured base URL, so credentials must not be embedded in
  URLs. Review error/log redaction, especially validation error details.
- No explicit arbitrary URL-fetch/upload/external code execution endpoint exists,
  but D2 is an indirect fetch path; do not claim SSRF is absent.
- Before API release: TLS/controlled ingress, trusted admission, limits, isolated
  secrets, controlled migrations, monitoring/readiness, bounded retention,
  backup/restore and rollback verification, independent security review.

## Hosting and environment plan

```text
Browser -> Vercel static frontend/curated docs
                   | deliberate link/proxy, only after API approval
SDK clients -------+----> API service -> private PostgreSQL <- worker
                                                             |
                                                     optional publishing
```

Development uses disposable local state; preview has no production credentials;
staging uses production-mode startup checks, separate PostgreSQL and worker,
synthetic data, faucet off and gossip off initially; production needs separate
secrets/data and operational approval. Keep API and continuous worker on a
container/service host suited to this lifecycle. Vercel static preview does not
require exposing the API. Domain purchase/canonical URLs/branding can wait.

The implementation sequence, SDK compatibility migration, deferred MCP/JS work
and exact file-level work packages are in [SDK_ARCHITECTURE_PLAN.md](SDK_ARCHITECTURE_PLAN.md).
