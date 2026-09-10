# ingest-service

Go service that polls ticketing sources and appends drop messages to the Redis
Stream. It is the only writer to `ticket:drops`.

Two independent tiers run in one process, supervised by an `errgroup` so that one
tier failing or being unconfigured does not take the other down:

| Tier | Source | Cadence | Emits |
| --- | --- | --- | --- |
| Discovery | EDMTrain | ~60 s | `announced` |
| Availability | RA | ~2 s, watchlisted events only | `new_ticketed`, `tier_available`, `returned_to_sale` |

RA additionally runs a slow **enumerate** loop (~15 min) that exists only to
learn RA event ids so the watchlist can reference them. It emits nothing:
appearing in a listing is not a sale-state change, and treating it as one would
alert on every event in a city on first run.

## Layout

```
cmd/ingest/            service entrypoint
cmd/mock-edmtrain/     stand-in API for running without a real key
cmd/mock-ra/           stand-in RA GraphQL, with admin hooks to trigger drops
internal/config/       environment loading and validation
internal/source/       normalized Event + Snapshot models, adapter contracts
internal/source/edmtrain/  EDMTrain client and adapter (discovery)
internal/source/ra/    RA GraphQL client and adapter (availability)
internal/fingerprint/  snapshot hashing and change classification
internal/resolve/      cross-source event matching (EDMTrain <-> RA)
internal/pool/         bounded worker pool for the fast loop
internal/cache/        Redis prior-snapshot store
internal/store/        Postgres: upserts, dedupe, watermark, watchlist
internal/stream/       Redis Stream publisher
internal/pipeline/     the discovery, enumerate, availability, and resolve loops
internal/metrics/      Prometheus instrumentation
```

## What RA can and cannot tell us

`Event.tickets` exists in RA's schema and carries exactly the fields a per-tier
detector would want, but it **returns an empty array to unauthenticated
clients** — verified across twelve events in three cities, with no GraphQL
error. Detection is therefore event-level, keyed on `isTicketed`, `cost`,
`presaleStatus`, `resaleActive`, and `status`. ARCHITECTURE.md §1.2 has the
evidence and the resulting fingerprint definition.

The practical consequence: ingestion detects that *something changed*, cheaply.
It does not know what is actually buyable. Resolving that is the browser stage's
job, and only for events that clear the hype threshold.

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

## Run the availability tier against the mock

`cmd/mock-ra` serves the two operations the adapter sends and exposes admin hooks
that mutate an event the way a real drop would, so the fast loop can be exercised
without polling ra.co every two seconds.

```powershell
cd ingest-service
go run ./cmd/mock-ra -addr :8098
```

In a second terminal:

```powershell
cd ingest-service
$env:RA_GRAPHQL_URL="http://localhost:8098/graphql"
$env:RA_AREAS="19"
$env:RA_DEV_SEED_WATCHLIST="5"   # dev only; the harness promotes by hype score
$env:EDMTRAIN_API_KEY=""          # discovery tier off for this run
go run ./cmd/ingest
```

Wait for `enumerate complete` and the watchlist seed, then trigger transitions:

```powershell
curl.exe -X POST "localhost:8098/admin/presale?id=90001"   # -> tier_available
curl.exe -X POST "localhost:8098/admin/resale?id=90003"    # -> returned_to_sale
curl.exe -X POST "localhost:8098/admin/flyer?id=90002"     # -> nothing, by design
```

The third one is the important case. It bumps only `dateUpdated`, so the
fingerprint moves but no rule classifies it. The change is written to
`event_snapshots` and no message is published, because alerting on flyer swaps is
how you train someone to ignore alerts.

The first poll of any event establishes a baseline and emits nothing. Without
that rule every watchlisted event would look like a fresh drop on startup.

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
rather than the network. Two invariants are worth calling out because they are
easy to break silently:

- `TestClassifiableTransitionsAlsoChangeFingerprint` — classification only runs
  after the hash moves, so any transition `Classify` recognizes must also change
  the fingerprint. Otherwise the rule is unreachable dead code.
- `TestComputeStability` asserts `attending` is *excluded* from the hash. It
  ticks constantly as people save an event, so including it would make the
  fingerprint change on nearly every poll and destroy its value as a cheap
  "nothing happened" filter.

The store has no unit tests; it is exercised by the two mock end-to-end runs
above against real Postgres and Redis.

## Operational notes

- `GET /metrics` on `:2112` (Prometheus), `GET /healthz` for liveness.
- `EDMTRAIN_POLL_INTERVAL_MS` is floored at 10 s in code. Their terms set rate
  limits at their discretion and forbid circumventing them.
- `POLL_INTERVAL_MS` (RA) is floored at 500 ms. RA publishes no limits, which is
  a reason for more restraint rather than less: an undocumented limit is enforced
  by an IP block.
- A 401/403 stops the discovery loop rather than retrying: a revoked key cannot
  be retried into working, and hammering it is what their terms prohibit.
- A missing `EDMTRAIN_API_KEY` or empty `RA_AREAS` disables that tier with a
  warning instead of crashing the other tier.
- `ingest_poll_dropped_total` above zero means the worker queue is shedding
  stale poll jobs — undersized `RA_WORKERS` or a slow upstream. Shedding is
  deliberate: a job queued for seconds is checking stale data, and the next tick
  produces a fresher one.
- `ingest_unclassified_change_total` climbing means the fingerprint includes a
  field that changes for reasons `Classify` does not model, burning polls.

## Cross-source resolution

The two tiers do not share an id space: an EDMTrain event id is meaningless to
RA. `internal/resolve` decides whether a show on one source is the same night as
a show on the other, scoring venue similarity (0.45), lineup overlap (0.40), and
title similarity (0.15), with the date as a hard filter rather than a weighted
term. Successful matches are written to `event_links`.

Candidates come from our own tables, not a fresh upstream query. The enumerate
loop has already stored every RA event in the configured areas, so resolution
costs one local SQL query instead of one API call per unresolved event.

Two decisions carry most of the weight:

- **A false link is worse than no link.** A wrong match means polling some other
  event and alerting on a drop the user cannot buy, which is indistinguishable
  from a bug. So a link needs both an absolute score (`AcceptScore` 0.60) and a
  clear margin over the runner-up (`AmbiguityMargin` 0.10). Residencies produce
  near-identical candidates on the same night at the same venue, and guessing
  between them is exactly the failure this refuses to commit.
- **Date tolerance is ±48 h, which is wider than it looks.** EDMTrain gives a
  date with no time, stored as midnight UTC; RA gives a real instant. A 22:00
  show in Detroit is 02:00 UTC the next day, so the same night is already 26 h
  apart before the sources disagree at all. A tighter bound silently rejects
  correct matches for precisely the late-night shows that matter here.

Lineup overlap uses the overlap coefficient rather than Jaccard: a thirty-act
festival bill that contains both of EDMTrain's two listed names is very likely
the same event, but Jaccard scores that near zero purely because the sets differ
in size.

The loop is unlinked-only and re-examines rejections on the next pass, since a
missing candidate now is not evidence that none exists — enumerate may simply not
have stored the counterpart yet.

Acting on the link is the harness's job: when it promotes an announced EDMTrain
event, it follows `event_links` to the RA counterpart and watchlists *that* row
with `watch_source = 'ra'`, which is what `DueWatchItems` requires. If the link
does not exist yet, it still writes the EDMTrain row (so the decision is not
lost) and a 30 s harness loop rewrites it once the resolver catches up. Writing
the watchlist entry against the EDMTrain row and leaving it there would create
something nothing polls.
