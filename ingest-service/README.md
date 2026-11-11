# ingest-service

Go 1.25 service that enumerates Resident Advisor events, polls the shared
watchlist, classifies observable sale-state changes, and publishes them to
Redis Stream `ticket:drops`. It is the only process that calls RA and the only
writer to that stream.

## Runtime loops

- **Enumeration** reads upcoming events for `RA_AREAS` on
  `RA_ENUMERATE_INTERVAL_MS` (default 15 minutes), upserts catalogue rows, and
  emits nothing.
- **Availability** reads active `watchlist` rows and polls them on
  `POLL_INTERVAL_MS` (default 5 seconds) through a bounded worker pool.

Enumeration is intentionally silent: a new catalogue listing is not evidence
that a sale state changed.

## Observable RA signal

Unauthenticated `Event.tickets` is empty for sampled `LEGACY` events, so this
service does not claim inventory or per-tier visibility. Its fingerprint covers
`isTicketed`, normalized `cost`, `presaleStatus`, `resaleActive`,
`queueItEnabled`, event `status`, and `dateUpdated`.

It publishes only:

- `presaleStatus` null → `ACTIVE` as `tier_available`;
- `isTicketed` false → true as `new_ticketed`;
- `resaleActive` false → true as `returned_to_sale`;
- normalized `cost` changing while ticketed as `tier_available`.

`dateUpdated` alone never classifies a change. `attending` and
`interestedCount` are excluded from the fingerprint. The first observation
writes `snap:ra:<event_id>` and a durable snapshot without publishing, which
prevents cold starts from alerting on every event.

## Layout

```text
cmd/ingest/             composition root and health/metrics server
cmd/mock-ra/            local GraphQL contract and mutation hooks
internal/config/        environment loading and validation
internal/source/ra/     RA client and adapter
internal/fingerprint/   stable hash and change classification
internal/pipeline/      enumeration and availability loops
internal/pool/          bounded polling workers
internal/cache/         Redis prior-snapshot storage
internal/store/         Postgres catalogue, snapshots, and watch reads
internal/stream/        Redis publisher
internal/metrics/       Prometheus instruments
```

## Run locally

Start Postgres and Redis from the repository root, then run the mock:

```powershell
docker compose up -d
cd ingest-service
go run ./cmd/mock-ra -addr :8098
```

In another terminal:

```powershell
cd ingest-service
$env:RA_GRAPHQL_URL="http://localhost:8098/graphql"
$env:RA_AREAS="19"
$env:RA_DEV_SEED_WATCHLIST="3"
go run ./cmd/ingest
```

Wait for enumeration and the silent baseline, then mutate a mock event:

```powershell
curl.exe -X POST "http://localhost:8098/admin/presale?id=90001"
curl.exe -X POST "http://localhost:8098/admin/resale?id=90003"
curl.exe -X POST "http://localhost:8098/admin/flyer?id=90002"
```

The flyer mutation changes `dateUpdated` but is not published. Inspect the
stream and durable observations:

```powershell
docker compose exec redis redis-cli XRANGE ticket:drops - + COUNT 5
docker compose exec postgres psql -U ticketdrop -d ticketdrop -c "SELECT fingerprint, observed_at FROM event_snapshots ORDER BY observed_at DESC LIMIT 5;"
```

`RA_DEV_SEED_WATCHLIST` is a development helper. In normal operation, the
Python subscription reconciler owns `watchlist`.

## Configuration and endpoints

- `DATABASE_URL` is required.
- `REDIS_URL` defaults to `redis://localhost:6379/0`.
- `REDIS_STREAM` defaults to `ticket:drops`.
- `RA_GRAPHQL_URL` defaults to `https://ra.co/graphql`.
- `RA_AREAS` enables ingestion and limits enumeration.
- `POLL_INTERVAL_MS` defaults to 5000 and must be at least 500.
- `POLL_TIMEOUT_MS` defaults to 4000.
- `RA_WORKERS`, `RA_QUEUE_SIZE`, and `RA_BATCH_SIZE` bound concurrency.
- `RA_ENUMERATE_INTERVAL_MS` defaults to 900000.

The service exposes `/metrics`, `/healthz`, and dependency-aware `/readyz` on
the ingest metrics port (2112 by default).

The worker queue deliberately sheds stale jobs when full. A later poll is more
useful than an old request waiting behind head-of-line work. Rising
`ingest_poll_dropped_total` means worker capacity or upstream latency needs
attention.

## Tests

```powershell
go test ./...
go vet ./...
gofmt -l .
```

Tests use local doubles and table-driven cases. The critical invariants are that
every classifiable transition changes the fingerprint, demand counters do not
change it, first sight is silent, and an unclassified delta is not published.
