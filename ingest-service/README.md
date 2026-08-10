# ingest-service

Go service that polls ticketing sources and appends drop messages to the Redis
Stream. It is the only writer to `ticket:drops`.

**Phase 2a implements the discovery tier only** — EDMTrain, ~60 s cadence,
emitting `announced`. The RA availability tier with fingerprint diffing and a
bounded worker pool is 2b.

## Layout

```
cmd/ingest/            service entrypoint
cmd/mock-edmtrain/     stand-in API for running without a real key
internal/config/       environment loading and validation
internal/source/       normalized Event model + adapter contract
internal/source/edmtrain/  EDMTrain client and adapter
internal/store/        Postgres: upserts, dedupe, watermark
internal/stream/       Redis Stream publisher
internal/pipeline/     the discovery poll loop
internal/metrics/      Prometheus instrumentation
```

## Run against the mock

No API key needed. From the repo root, with `docker compose up -d` already
running:

```powershell
cd ingest-service
go run ./cmd/mock-edmtrain -addr :8099
```

In a second terminal:

```powershell
cd ingest-service
$env:EDMTRAIN_API_KEY="mock-key"
$env:EDMTRAIN_BASE_URL="http://localhost:8099/api"
$env:EDMTRAIN_POLL_INTERVAL_MS="10000"
go run ./cmd/ingest
```

The first poll announces every seeded event; later polls announce nothing,
because dedupe is enforced by `UNIQUE (source, source_event_id)`. To watch a
single new announcement flow through:

```powershell
Invoke-WebRequest -Uri http://localhost:8099/admin/add -Method POST
```

Inspect the results:

```powershell
docker compose exec redis redis-cli XRANGE ticket:drops - + COUNT 5
docker compose exec postgres psql -U ticketdrop -d ticketdrop -c "SELECT source_event_id, title FROM events;"
```

## Run against the real API

Set `EDMTRAIN_API_KEY` in `.env` and leave `EDMTRAIN_BASE_URL` at its default.
`EDMTRAIN_LOCATION_IDS` should be numeric ids from their Locations API:

```
GET https://edmtrain.com/api/locations?state=Michigan&city=Detroit&client=<key>
```

Leaving it empty makes the API return every upcoming event, which is slow and
wasteful; the service warns at startup if so.

## Tests and checks

```powershell
go test ./...
go vet ./...
gofmt -l .
```

Tests are table-driven per `.cursor/rules/system-arch.mdc` and use `httptest`
rather than the network. The store and publisher have no unit tests yet — they
are exercised by the mock end-to-end run above, and get integration tests in 2b
once there are two adapters to share fixtures.

## Operational notes

- `GET /metrics` on `:2112` (Prometheus), `GET /healthz` for liveness.
- `EDMTRAIN_POLL_INTERVAL_MS` is floored at 10 s in code. Their terms set rate
  limits at their discretion and forbid circumventing them.
- A 401/403 stops the loop rather than retrying: a revoked key cannot be
  retried into working, and hammering it is what their terms prohibit.
- A missing `EDMTRAIN_API_KEY` disables the adapter with a warning instead of
  crashing, matching how DICE is treated.
