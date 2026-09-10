# TicketDrop

Low-latency detection of unannounced house-music ticket drops across Resident
Advisor and EDMTrain, with vector-based hype scoring and a bounded,
self-healing browser assist stage.

Design decisions, the state machine, the latency budget, and the failure-mode
analysis all live in **[`ARCHITECTURE.md`](./ARCHITECTURE.md)**. Read that first.

Ingestion runs in two tiers. **Discovery** polls EDMTrain once a minute using its
`createdStartDate` change feed to learn what shows exist. **Availability** polls
Resident Advisor every two seconds against a watchlist, fingerprinting each
event's ticket state to catch a sale going live. They answer different questions
and neither replaces the other.

## Layout

```
.cursor/rules/          architectural boundaries + Socratic interview mode
ARCHITECTURE.md         system design contract
db/init/                Postgres + pgvector schema (runs on first boot)
deploy/observability/   Prometheus config for the Go service
docker-compose.yml      Redis Streams + pgvector, observability behind a profile
ingest-service/         Go discovery and availability pollers
agent-harness/          Python LangGraph FSM and browser assist
admin-panel/            Next.js live operations panel
```

## Quickstart

```powershell
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

With Go service metrics (Prometheus on http://localhost:9090):

```powershell
docker compose --profile observability up -d
```

Verify pgvector and the schema landed:

```powershell
docker compose exec postgres psql -U ticketdrop -d ticketdrop -c "\dt"
docker compose exec postgres psql -U ticketdrop -d ticketdrop -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

Verify Redis Streams:

```powershell
docker compose exec redis redis-cli XADD ticket:drops '*' probe 1
docker compose exec redis redis-cli XLEN ticket:drops
docker compose exec redis redis-cli DEL ticket:drops
```

The schema in `db/init/` is applied **only when the Postgres volume is empty**.
After editing it during development, reset with:

```powershell
docker compose down -v; docker compose up -d
```

Install application dependencies once:

```powershell
cd agent-harness
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd ..\admin-panel
npm ci
cd ..
```

## Running the full pipeline locally

Five processes, no API keys. The mock sources serve the same contracts as the
real ones, so this exercises every stage end to end.

```powershell
# 1-2. Mock sources
cd ingest-service
go run ./cmd/mock-edmtrain -addr :8099
go run ./cmd/mock-ra       -addr :8098

# 3. Ingest, pointed at the mocks. RA_DEV_SEED_WATCHLIST gives the
#    availability loop something to poll before the harness promotes anything.
$env:EDMTRAIN_API_KEY="mock-key"; $env:EDMTRAIN_BASE_URL="http://localhost:8099/api"
$env:EDMTRAIN_LOCATION_IDS="1"; $env:EDMTRAIN_POLL_INTERVAL_MS="10000"
$env:RA_GRAPHQL_URL="http://localhost:8098/graphql"; $env:RA_AREAS="19"
$env:RA_ENUMERATE_INTERVAL_MS="20000"; $env:RESOLVE_INTERVAL_MS="20000"
$env:RA_DEV_SEED_WATCHLIST="3"
go run ./cmd/ingest

# 4. Harness. Seed preferences first or every drop scores zero.
cd ../agent-harness
.\.venv\Scripts\python.exe -m harness prefs add --kind artist --label "Prospa"
.\.venv\Scripts\python.exe -m harness run

# 5. Panel
cd ../admin-panel; npm install; npm run dev   # http://localhost:3000
```

Nothing happens until a source *changes* — the first sighting of an event
records a baseline rather than emitting a drop, which is what stops the poll
loop from reporting every watched event as a fresh drop on startup. Provoke one:

```powershell
curl.exe -X POST "http://localhost:8098/admin/presale?id=90001"   # presale opens
curl.exe -X POST "http://localhost:8098/admin/ticketed?id=90002"  # tickets appear
curl.exe -X POST "http://localhost:8099/admin/add"                # new announcement
```

## Going live (personal)

Copy `.env.example` to `.env` at the repo root. All three services read that file.

| Variable | Required? | Where to get it | What happens if empty |
| --- | --- | --- | --- |
| `EDMTRAIN_API_KEY` | **Yes** for discovery | Apply at [edmtrain.com/developer-api](https://edmtrain.com/developer-api) while signed in | Discovery tier disables; you only see RA events you seed or already watch |
| `EDMTRAIN_LOCATION_IDS` | **Yes** with the key | `GET https://edmtrain.com/api/locations?state=Michigan&city=Detroit&client=<key>` | The API returns every upcoming event, which is slow and wasteful |
| `RA_AREAS` | **Yes** for availability | City code in `ra.co/events/...` URLs (Detroit `19`, London `13`, Berlin `34`, NYC `8`) | Availability tier disables. No key needed |
| `VOYAGE_API_KEY` | Recommended | [voyageai.com](https://www.voyageai.com/) | Exact artist-name matches still score 1.0; similar names score noise |
| `ANTHROPIC_API_KEY` | No (`alert` mode) | [console.anthropic.com](https://console.anthropic.com/) | Self-heal uses canned selectors. Irrelevant unless you switch to `assist` |

Then seed the artists you actually care about — an empty preference table scores every drop at zero:

```powershell
cd agent-harness
.\.venv\Scripts\python.exe -m harness prefs add --kind artist --label "Prospa"
```

Keep http://localhost:3000 open. There is no push notification; the panel is the alert.

Running the discovery tier against the live API needs an EDMTrain client key,
which you apply for while signed in at
[edmtrain.com/developer-api](https://edmtrain.com/developer-api). Until it
arrives, `ingest-service/cmd/mock-edmtrain` serves the same contract locally —
see [`ingest-service/README.md`](./ingest-service/README.md).

## Going public

This repo is private during development and intended to go public later. Two
things to know before flipping that switch, because both are hard to undo:

**Git history is permanent.** Making a repo public exposes every commit ever
made, not just the current tree. A secret committed once and deleted later is
still in the history and still readable. `.env` is gitignored from the first
commit for that reason — keep it that way, and never commit a real EDMTrain key,
Voyage key, or Anthropic key even temporarily.

**What is deliberately excluded.** `.cursor/rules/interview-prep.mdc` is
gitignored: it is personal context rather than architecture. `system-arch.mdc`
*is* tracked on purpose — enforced architectural boundaries are part of how this
project is built, and worth showing.

## Operating posture

`RESERVATION_MODE` defaults to `alert`: detect, score, and notify, with no
automated purchase path. See [`ARCHITECTURE.md` §10](./ARCHITECTURE.md) before
changing it — automated ticket purchasing is regulated by the US BOTS Act and
the UK's Breaching of Limits on Ticket Sales Regulations 2018, and events behind
a Queue-it waiting room are forced to `alert` by design.

EDMTrain's API terms add three constraints, covered in
[`ARCHITECTURE.md` §10.1](./ARCHITECTURE.md): its event links must be displayed
unmodified, rate limits may not be circumvented, and its data may not be
combined with other sources to build a competing public event-discovery service.
That last clause is fine for a private tool and is a real blocker for a
multi-user product.
