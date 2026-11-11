# Production deployment

The v1 topology is one 2-vCPU/4-GB Linux VPS. Caddy is the only public
container. Postgres and Redis are on an internal Docker network; ingest and the
harness receive outbound internet access but publish no host ports.

This is a low-cost portfolio deployment, not high availability. Confirm the
current Resident Advisor terms before enabling live polling. Use replay mode
when public polling is not authorized.

## Provision

1. Point an `A`/`AAAA` record at the VPS.
2. Install Docker Engine with the Compose plugin.
3. Clone the repository and copy `.env.production.example` to
   `.env.production`.
4. Generate independent random values for Postgres, Redis, Auth.js, and
   unsubscribe signing. Configure one OAuth provider and a verified Resend
   sender.
5. Keep one metro, a 5-second interval, 6 workers, and at most 100 unique
   watched events until measurements justify a higher limit.

```sh
docker compose --env-file .env.production -f docker-compose.prod.yml config
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml ps
```

Caddy obtains and renews TLS automatically. OAuth callback URLs are:

- `https://DOMAIN/api/auth/callback/github`
- `https://DOMAIN/api/auth/callback/google`

## Database bootstrap

This release has no production database to upgrade. A fresh Postgres volume
uses `db/init/001_schema.sql`; initialization scripts run only when the volume
is empty. Back up before future schema changes and add an explicit migration
once production data exists.

## Backup and restore

Run `backup-postgres.sh` nightly from cron and copy dumps off the VPS. A backup
on the same disk is not a disaster-recovery copy.

```cron
17 3 * * * cd /opt/ticketdrop && BACKUP_DIR=/var/backups/ticketdrop ./deploy/scripts/backup-postgres.sh
```

Quarterly, restore the newest dump into a disposable VPS or database and run
the integration smoke test. `restore-postgres.sh` is intentionally interactive
because it replaces the target database.

## Operations

- `ingest:2112/healthz` is process liveness; `/readyz` checks Postgres and Redis.
- `harness:2113/healthz` is process liveness; `/readyz` checks Postgres and Redis.
- Docker restarts failed processes. Postgres remains the durable audit record.
- Alert on disk above 80%, repeated container restarts, ingest poll errors,
  Redis memory/noeviction errors, and pending notification deliveries older
  than five minutes.
- Rotate OAuth, Resend, Redis, Postgres, Auth.js, and unsubscribe secrets after
  any suspected disclosure.

## Cost and scaling boundary

A small VPS is expected to cost roughly $10–15/month, excluding a domain and
email overages. Move Postgres/Redis to managed services only when the product
needs host-level failover or the unique event watchlist reaches the low
hundreds with sustained resource pressure. User count alone is not the polling
scale variable; unique watched events are.
