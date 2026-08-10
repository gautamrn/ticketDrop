# TicketDrop

Low-latency detection of unannounced house-music ticket drops across Resident
Advisor, EDMTrain, and DICE, with vector-based hype scoring and a bounded,
self-healing browser execution stage.

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
deploy/observability/   OTel Collector and Prometheus config
docker-compose.yml      Redis Streams + pgvector, observability behind a profile
ingest-service/         Go poller            (phase 2)
agent-harness/          Python LangGraph FSM (phase 3)
admin-panel/            Next.js live panel   (phase 5)
```

## Quickstart

```powershell
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

With telemetry (Prometheus on http://localhost:9090):

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

## Phases

| Phase | Deliverable | Status |
| --- | --- | --- |
| 1 | Rules, architecture, compose stack, pgvector schema | done |
| 2a | Go ingest: EDMTrain discovery adapter + Redis `XADD` | done |
| 2b | Go ingest: RA availability adapter, diff engine, watchlist scheduler | next |
| 3 | Python harness: LangGraph FSM, hype scoring, Playwright | |
| 4 | Self-healing DOM repair loop | |
| 5 | Next.js admin panel over the results stream | |

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
Anthropic key, or DICE token even temporarily.

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
