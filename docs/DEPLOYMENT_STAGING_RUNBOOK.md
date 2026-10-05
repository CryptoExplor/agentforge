# Protected staging deployment runbook

This recipe runs PostgreSQL 16, an explicitly migrated AgentForge API and the
outbox/claim-reaper worker. It is a **mock-credit, pre-testnet staging** recipe:
no chain adapter exists, the production mock faucet is forbidden, enrollment is
closed, operator self-grants are disabled and external publishing is off.

## 1. Host preparation

- Use a supported Linux host with Docker Engine + Compose v2, an NTP client,
  host firewall and TLS reverse proxy. Do not commit host IPs or DNS names.
- Create an unprivileged `agentforge` user and install the checkout at
  `/opt/agentforge`; only that user and administrators should read
  `deploy/.env.staging`.
- Permit inbound 443 at the firewall. The Compose default binds API port 8080 to
  loopback only; proxy HTTPS to `127.0.0.1:8080`. Do not expose PostgreSQL.
- Keep Docker socket access limited: membership is effectively root.

```bash
cp deploy/env.staging.example deploy/.env.staging
chmod 600 deploy/.env.staging
# Fill random POSTGRES_PASSWORD and a secret-manager generated signing seed.
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml config --quiet
```

A URL-safe database password is required by the current Compose interpolation.
Never put real secrets in shell history, source control or support logs.

## 2. Start and verify

Create a backup before every upgrade. Then build and start; `migrate` must exit
successfully before API/worker startup.

```bash
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml up --build -d --wait
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml ps -a
curl --fail --silent http://127.0.0.1:8080/health | python -m json.tool
```

Require `status: ok`, a populated `clock.database_time`, no `database_error`,
and absolute `database_skew_seconds` no greater than
`database_skew_tolerance_seconds`. Fix NTP/host time rather than widening the
limit casually. Also verify migration state and logs:

```bash
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml run --rm migrate alembic current
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml logs --tail=200 api worker
```

The defaults enforce the protected-staging gate:

- `AGENTFORGE_REGISTRATION_OPEN=false` (pre-enroll approved identities);
- `OPEN_OPERATORS=false`; validator capability declarations grant no authority;
- every validator requires both `AGENTFORGE_TRUSTED_VALIDATOR_DIDS` and an
  operator-attributed `ACTIVE` `operator_role_grants` row;
- faucet and Technocore/gossip transport disabled; and
- only local mock settlement assets enabled.

There is intentionally no public admin/faucet endpoint. Pre-enrollment, mock
balance provisioning and role grants are controlled operator procedures and
must be recorded in the staging audit record. Do not turn production mode into
an open faucet to run a demo. `scripts/simulate_marketplace.py` can reuse
pre-enrolled identity files; its deterministic mode does not grant validator
power. See `docs/OPERATOR_REGISTRY.md`.

## 3. systemd operation

Review paths/users, then install the supplied templates:

```bash
sudo install -m 0644 deploy/systemd/agentforge-staging.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/agentforge-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/agentforge-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentforge-staging.service agentforge-backup.timer
systemctl status agentforge-staging.service agentforge-backup.timer
```

## 4. Backup and restore drill

Make a private custom-format backup and copy it off-host to encrypted storage.
A backup is not accepted until a restore into an isolated database has passed
`alembic current` and `/health` verification.

```bash
umask 077
mkdir -p backups
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml exec -T postgres \
  pg_dump -U agentforge -d agentforge -Fc > \
  "backups/agentforge-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Restore only during a maintenance window after preserving the failed database:

```bash
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml stop api worker
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml exec -T postgres \
  dropdb -U agentforge --if-exists agentforge
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml exec -T postgres \
  createdb -U agentforge agentforge
cat backups/SELECTED.dump | docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml exec -T postgres \
  pg_restore -U agentforge -d agentforge --clean --if-exists --no-owner
docker compose --env-file deploy/.env.staging \
  -f deploy/docker-compose.staging.yml up -d --wait
```

## 5. Application rollback

Prefer restoring the previous application image/commit while retaining a
forward-compatible schema. Before release, inspect migration downgrade code and
practice it on a restored copy. If schema rollback is unavoidable:

1. stop API and worker;
2. take and verify a final backup;
3. run `alembic history` and identify the exact previous revision;
4. run `docker compose ... run --rm migrate alembic downgrade <revision>` only
   after confirming it does not destroy data needed for recovery; and
5. deploy the matching prior application, start, and repeat health/contracts
   checks.

Never run an unreviewed `downgrade -1` against the only copy of staging data.
For destructive or uncertain migrations, database restore is the rollback.

## 6. Stop and incident boundary

`docker compose ... down` preserves the named PostgreSQL volume; never add `-v`
during ordinary operation. On clock failure, accounting discrepancy, leaked
key, uncertain settlement or migration error, stop API/worker, preserve logs and
backup, and escalate. There is no live-chain adapter to retry or reconcile;
external settlement remains blocked by
`EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md`.
