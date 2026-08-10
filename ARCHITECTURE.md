# TicketDrop — Architecture

Autonomous, low-latency detection of unannounced house-music ticket drops
across Resident Advisor, EDMTrain, and DICE, with vector-based hype scoring and
a bounded, self-healing browser execution stage.

---

## 1. Source reality

The blueprint assumed the sources can be polled symmetrically. They cannot.
This is the single most important constraint on the ingestion design.

| Source | Endpoint | Auth | What you actually get |
| --- | --- | --- | --- |
| **Resident Advisor** | `POST https://ra.co/graphql` | None | The site's own public GraphQL API. `eventListings(filters, pageSize, page, sort)` filtered by `areas.eq` (numeric city code) and `listingDate.gte/lte`. Introspection is enabled. Requires a normal browser `User-Agent`. |
| **EDMTrain** | `GET https://edmtrain.com/api/events` | `client=<API key>` query param, free, applied for with an account | Sanctioned listings API for US/Canada. Filter by `locationIds`, `artistIds`, `venueIds`, `startDate`, and critically **`createdStartDate`**. Returns `ticketLink`, lineup with `b2bInd`, venue with coordinates. No images, **no ticket availability**. |
| **DICE** | `POST https://partners-endpoint.dice.fm/graphql` | Bearer token issued in MIO | The **Ticket Holders** partner API. Cursor-paginated `viewer.events`. It returns *the events belonging to your own partner account*, not a global catalogue. |

Consequences:

- **DICE cannot be used for global discovery.** Without a partner token there is
  no legitimate DICE feed. The adapter is therefore built but disabled unless
  `DICE_PARTNER_TOKEN` is set, and even then it only covers your own events.
- Treat RA's field names as *modeled, not contracted* — it is the site's own
  internal API with no stability guarantee and no agreement behind it. EDMTrain
  is the only source with a real API key and written terms. Schema drift is a
  first-class failure mode (§9), which is why the ingest layer stores the raw
  payload alongside parsed columns.

### 1.1 Two tiers: discovery and availability

EDMTrain and RA answer different questions, so they run as separate tiers with
separate cadences rather than as interchangeable adapters.

| | Discovery tier | Availability tier |
| --- | --- | --- |
| Source | EDMTrain | RA (DICE if tokened) |
| Question | "What shows exist?" | "Did the sale state change?" |
| Mechanism | `createdStartDate` = last poll | Fingerprint diff (§1.2) |
| Cadence | ~60 s | ~2 s, watchlisted events only |
| Emits | `announced` | `new_ticketed`, `tier_available`, `returned_to_sale` |

`createdStartDate` is the reason EDMTrain earns a place. It means "events added
to EDMTrain at or after this UTC timestamp", which is a **server-side change
feed** — we ask for the delta instead of reconstructing it. No fingerprints, no
prior state, no wasted comparisons. RA offers no equivalent, which is precisely
why RA needs the diff machinery below.

The tiers compose: EDMTrain discovers a show days before it would enter our RA
watch window, hype scoring decides whether it deserves a fast-poll slot, and it
gets promoted into `watchlist` for second-by-second availability monitoring.
EDMTrain's US/Canada coverage is also materially better than RA's for American
markets, which is where most of the discovery gap was.

**What EDMTrain cannot do:** it is a listings site, not a ticketing platform. It
has no notion of ticket tiers, sale state, or inventory. It tells you a show
exists and links out. Detecting the *unannounced drop* in the project's premise
remains RA's job.

### 1.2 What "a drop" actually is

There is no drop webhook. An unannounced drop is a **diff between two polls**, so
the ingest service is a change-detector, not a listener. For each tracked event
we compute an availability fingerprint:

```
fingerprint = sha256(event_id ‖ sorted(tier_name:tier_state:price)... ‖ is_ticketed)
```

A drop is emitted when the fingerprint transitions in a meaningful direction:

- an event appears in a watched area for the first time and is already ticketed;
- `isTicketed` flips `false → true`;
- a ticket tier appears, or moves `sold_out → available` (a release or a return).

The previous fingerprint lives in Redis (`fp:<source>:<event_id>`, TTL 30 days).
Postgres holds the durable history. Fingerprint equality is the dedupe primitive
that stops a 2-second poll loop from emitting the same drop 30 times a minute.

---

## 2. Component diagram

```
   DISCOVERY TIER (~60 s)              AVAILABILITY TIER (~2 s)
  [ EDMTrain REST + key ]        [ RA GraphQL ]   [ DICE partner GraphQL ]
             │                          └────────────┬─────────┘
             │ createdStartDate delta                │ fingerprint diff
             ▼                                       ▼
  ┌────────────────────────┐            ┌──────────────────────────┐
  │  Discovery poller      │──promote──▶│   Availability poller    │
  │  emits 'announced'     │  watchlist │   emits sale changes     │
  └───────────┬────────────┘            └────────────┬─────────────┘
              └──────────────┬───────────────────────┘
                             ▼
                        ┌──────────────────────────┐
                        │     Go Ingest Proxy      │  p99 append < 50 ms
                        └────────────┬─────────────┘
                                     ▼
                        ┌──────────────────────────┐
                        │  Redis Stream            │
                        │  'ticket:drops'          │  XADD / consumer group
                        └────────────┬─────────────┘
                                     ▼
                        ┌──────────────────────────┐
                        │  LangGraph FSM Harness   │
                        │  (Python, async)         │
                        └────┬───────────────┬─────┘
                             │               │
              hype scoring   ▼               ▼   execution
        ┌────────────────────────┐   ┌────────────────────────┐
        │ Postgres + pgvector    │   │ Playwright Browser     │
        │ similarity match       │   │ Engine (bounded)       │
        └────────────────────────┘   └───────────┬────────────┘
                             │                   │ selector miss
                             │                   ▼
                             │       ┌────────────────────────┐
                             │       │ AST Self-Heal (Claude) │
                             │       │ 1 retry, cached to     │
                             │       │ selector_overrides:*   │
                             │       └───────────┬────────────┘
                             └───────────┬───────┘
                                         ▼
                        ┌──────────────────────────┐
                        │ Redis 'ticket:drops:     │
                        │  results' → Next.js WS   │
                        └──────────────────────────┘
```

---

## 3. Latency budget

Measured end-to-end from the poll response byte that first contained the change.

| Stage | Budget | Owner |
| --- | --- | --- |
| Ingest: parse, diff, fingerprint, `XADD` | 0 – 50 ms | Go |
| Evaluate: embed, pgvector match, hype score | 50 – 350 ms | Python |
| Reserve: browser navigate → hold/handoff | 350 – 2500 ms | Playwright |
| Self-heal (conditional, one attempt only) | +1000 ms max | Claude |
| **Total worst case** | **≈ 3.5 s** | |

The budget excludes *poll interval*, which dominates real-world detection lag.
At a 2 s interval the expected detection delay is ~1 s (half the interval), which
is 300× the entire ingest budget. Optimizing ingest below 50 ms is therefore
about not adding jitter under load, not about winning the race by itself.

Each stage writes its measured `latency_ms` into `drop_transitions`. Budget
violations are a Prometheus alert, not a silent slowdown.

---

## 4. Finite state machine

```
        ┌───────────┐
        │ INGESTED  │  message read from stream, claimed by consumer group
        └─────┬─────┘
              ▼
        ┌───────────┐
        │ EVALUATING│  embed + pgvector similarity → hype_score ∈ [0,1]
        └─────┬─────┘
              │
      ┌───────┴────────┐
      │ score < 0.80   │ score ≥ 0.80
      ▼                ▼
 ┌─────────┐     ┌───────────┐
 │ SKIPPED │     │ RESERVING │  Playwright: navigate, locate CTA, hold
 └─────────┘     └─────┬─────┘
   (terminal)          │
              ┌────────┴─────────┐
              │ selector miss    │ success
              ▼                  ▼
        ┌───────────┐      ┌───────────┐
        │ SELF_HEAL │─────▶│ NOTIFIED  │  (terminal)
        └─────┬─────┘  ok  └───────────┘
              │ repair failed / retry budget spent
              ▼
        ┌───────────┐
        │  FAILED   │  (terminal) → manual alert to admin panel
        └───────────┘
```

Two states are added to the original blueprint on purpose:

- **`SKIPPED`** — the blueprint's FSM had no exit for a sub-threshold hype score.
  Without it, ~95% of ingested drops have nowhere to go and the graph either
  leaks or misroutes them into `RESERVING`.
- **`FAILED`** — self-healing must have a terminal sink, otherwise a repair loop
  can re-enter `RESERVING` indefinitely and starve the worker pool.

**Discovery messages take a shortened path.** A `change == "announced"` message
runs `INGESTED → EVALUATING` like any other, but the branch after scoring is
watchlist promotion, not reservation: above threshold it inserts into
`watchlist` (tier `hot`) and terminates in `NOTIFIED`; below threshold it
terminates in `SKIPPED`. It never enters `RESERVING`, because at announcement
time there is nothing to reserve — EDMTrain does not know whether tickets are
on sale. Reservation only ever fires from an availability-tier change.

Invariants enforced by the harness:

1. `SELF_HEAL` is entered **at most once** per drop (`attempt_count <= 1`).
2. Every state has a wall-clock deadline; expiry routes to `FAILED`, never to a
   retry.
3. Every transition is appended to `drop_transitions` before the next node runs,
   so a crashed worker's last known state is always recoverable.

---

## 5. Redis Streams contract

| Key | Type | Purpose |
| --- | --- | --- |
| `ticket:drops` | stream | Drop events. Written only by Go. `MAXLEN ~ 100000`. |
| `ticket:drops:results` | stream | FSM outcomes, tailed by the Next.js gateway. |
| `tdrop-harness` | consumer group on `ticket:drops` | Competing consumers, one per harness worker. |
| `fp:<source>:<event_id>` | string | Last availability fingerprint, TTL 30d. |
| `selector_overrides:<domain>` | hash | `logical_name → css_selector`, healed by Claude. |

Message fields on `ticket:drops` (flat, string-valued — Redis streams have no
nested types):

```
drop_id        uuid v7, generated by Go, idempotency key for the whole pipeline
source         "ra" | "edmtrain" | "dice"
event_id       source-native event id
event_url      canonical public URL (EDMTrain: its link, unmodified — ToS §10)
ticket_link    outbound vendor sale URL, when the source provides one
title          event title
venue          venue name
starts_at      RFC3339
artists        comma-separated names
attending      integer, RA interest signal (absent for EDMTrain)
queue_it       "true" | "false"
change         "announced" | "new_ticketed" | "tier_available" | "returned_to_sale"
fingerprint    sha256 hex of the new availability state (empty for "announced")
detected_at    RFC3339 with nanoseconds
trace_id       W3C traceparent, propagated into the Python span
raw            JSON blob of the source payload
```

One stream carries both tiers. An `announced` message is not a drop — it is a
candidate — and the FSM routes it to watchlist promotion rather than to
`RESERVING`. A second stream would double the consumer-group and ordering
surface to separate two message types that share every downstream stage up to
the branch.

**Delivery semantics: at-least-once.** Consumer groups plus `XACK` cannot give
exactly-once across a crash between side effect and ack. `drop_id` is therefore
an idempotency key: the harness takes `SET drop:claim:<drop_id> NX EX 900` before
any browser action, so a redelivered message after a worker crash cannot double
-submit a hold. `XAUTOCLAIM` reaps entries idle > 60 s from dead consumers.

`maxmemory-policy` is pinned to `noeviction`. Under an eviction policy Redis will
silently drop stream entries as memory fills — during exactly the traffic spike
you built this for.

---

## 6. Hype scoring and vectors

`hype_score ∈ [0,1]` is a weighted blend, not a single model output, so that a
missing signal degrades the score instead of erroring the node:

| Signal | Weight | Source |
| --- | --- | --- |
| Artist affinity | 0.40 | max cosine similarity between lineup artists' embeddings and the user's preference vectors |
| Sound signature match | 0.25 | cosine over a 12-dim numeric vector (BPM band, key as sin/cos, energy, danceability, …) |
| Venue affinity | 0.20 | cosine over venue preference vectors |
| Demand velocity | 0.15 | normalized `attending` growth per hour since first seen |

Threshold for `RESERVING` is `HYPE_THRESHOLD` (default `0.80`).

**Embedding model.** Anthropic does not expose an embeddings endpoint — Claude
cannot produce these vectors. The schema is dimensioned for **Voyage AI
`voyage-3` (1024-d)**, Anthropic's recommended embedding partner. Swapping to
OpenAI `text-embedding-3-small` means changing `VECTOR_DIM` to 1536 and
reindexing; the dimension is fixed at table-creation time, so decide before
loading data.

Indexes are **HNSW with cosine distance** (`vector_cosine_ops`). HNSW over IVFFlat
because IVFFlat requires a populated table to train its lists and degrades badly
when the corpus grows past the trained centroids; HNSW builds incrementally,
which matches a preference set that the user edits from the admin panel.

---

## 7. Self-healing protocol

Triggered only by a **selector miss** — a locator timeout with a 200-class page.
Network errors, 5xx, and auth walls route straight to `FAILED`; they are not
selector problems and asking an LLM about them just burns the latency budget.

1. Playwright captures the page HTML and strips `<script>`, `<style>`, `<svg>`,
   inline event handlers, and all attributes except `id`, `class`, `role`,
   `aria-*`, `data-testid`, `type`, and text content. This typically cuts a
   checkout page from ~800 KB to under 40 KB, which is both a cost and a latency
   control.
2. The cleaned DOM plus the failing `logical_name` (e.g. `buy_button`) goes to
   Claude 3.5 Sonnet, which returns candidate CSS selectors as structured JSON.
3. The top candidate is validated in-page (exactly one match, visible, enabled)
   before use. An unvalidated selector is never executed.
4. On validation success, write through to `selector_overrides:<domain>` in Redis
   **and** the `selector_overrides` table, then resume `RESERVING` once.
5. On failure, transition to `FAILED` and raise a manual alert. **One attempt.**

The retry cap is the whole safety property. Each heal costs ~1 s and a Sonnet
call; an uncapped loop against a site-wide redesign would occupy every worker
simultaneously and convert a cosmetic breakage into a total outage.

---

## 8. Persistence

`db/init/001_schema.sql` is the source of truth. Summary:

| Table | Role |
| --- | --- |
| `venues`, `artists`, `events`, `event_artists` | Normalized catalogue from source adapters. `artists` and `events` carry embeddings. |
| `event_links` | Cross-source entity resolution: duplicates of one show point at an elected canonical row. |
| `watchlist` | Which events the fast availability loop polls, and at what cadence tier. |
| `ticket_tiers` | Time-series of per-tier availability; the raw material for drop diffing. |
| `drops` | One row per detected drop = the FSM instance, including `state` and `hype_score`. |
| `drop_transitions` | Append-only FSM audit log with per-stage `latency_ms`. |
| `user_preferences` | The vectors hype scoring matches against. |
| `selector_overrides` | Durable copy of healed selectors; Redis is the cache. |

`events` and `ticket_tiers` keep a `raw jsonb` column. When an upstream schema
changes, a reparse is a SQL migration rather than lost data.

---

## 9. Failure modes

| # | Failure | Blast radius | Mitigation |
| --- | --- | --- | --- |
| 1 | RA changes its GraphQL schema | Total ingest blindness, silent | Contract test against live introspection on a schedule; alert on parse-error rate, not just on request errors. `raw` jsonb allows backfill. |
| 2 | Rate limiting / IP block from aggressive polling | Total ingest blindness, hard to reverse | Per-source token bucket, jittered intervals, exponential backoff on 429, single shared client with connection reuse. Poll interval is config, not code. |
| 3 | Thundering herd on a big drop | Every worker launches a browser at once; memory exhaustion | Bounded worker pool with a hard concurrency ceiling on browser contexts; queue depth is a Prometheus gauge with an alert. Playwright contexts, not processes, per drop. |
| 4 | Poison message | One bad payload crash-loops a consumer, blocking the group | Per-message `try/except` → `FAILED`; `XAUTOCLAIM` after 60 s idle; dead-letter after 3 deliveries. |
| 5 | Redis loss | Fingerprints and in-flight stream entries gone | AOF `everysec`; on cold start, treat all events as *seen* for one cycle to avoid re-emitting the entire catalogue as new drops. |
| 6 | Self-heal returns a plausible but wrong selector | Clicks the wrong control on a real checkout | Mandatory in-page validation (single match, visible, enabled) plus a semantic check on the element's text before click. |
| 7 | Clock skew between services | `latency_ms` becomes fiction | Latency measured from the timestamp minted by Go and carried in the message, single clock domain per drop. |
| 8 | EDMTrain revokes the API key for exceeding an undisclosed limit | Permanent loss of the discovery tier, and it is a human relationship to repair | Conservative fixed cadence (60 s), never burst; `createdStartDate` keeps each response small; treat 4xx as a stop-and-alert, not a retry. Their terms forbid working around limits, so backoff means backoff. |
| 9 | Cross-source duplicate: same show from EDMTrain and RA | Two alerts for one event, or a split hype score | `event_links` elects a canonical row; alerting keys on the canonical id. Low-confidence matches surface in the admin panel rather than auto-merging. |

Failure 8 is qualitatively different from the others. Every other row is a
technical fault that a retry or a redeploy fixes. A revoked key is an account
action by a company we have an agreement with, so the mitigation is behavioral
restraint, not engineering.

---

## 10. Operating posture (read before Phase 3)

Automated ticket *purchasing* sits in a regulated area: the US **BOTS Act**
(2016) and the UK's **Breaching of Limits on Ticket Sales Regulations 2018**
prohibit circumventing security measures, CAPTCHAs, waiting rooms, or per-person
purchase limits on ticket sellers. RA surfaces `queueItEnabled` precisely because
some events sit behind a Queue-it waiting room.

The reservation stage is therefore governed by `RESERVATION_MODE`:

| Mode | Behavior |
| --- | --- |
| `alert` *(default)* | No browser purchase path. Detect, score, notify with a deep link. Fastest to build, zero legal exposure. |
| `assist` | Launch a **headed** browser, navigate to the event, pre-fill known fields, then hand the live session to the human for CAPTCHA, queue, and payment. The bot never completes a transaction. |
| `auto` | Fully autonomous hold. **Requires** `queue_it == false` and a source whose ToS permits it. Off by default and not implemented in Phase 1. |

Events with `queue_it == true` are forced to `alert` regardless of mode. This is
an architectural constraint, not a runtime preference.

### 10.1 EDMTrain API terms

Three clauses constrain the design directly:

1. **"You may not combine our events with other event sources to create a
   competing event discovery service."** TicketDrop combines EDMTrain with RA by
   construction. For a private single-user tool this is very unlikely to be what
   the clause is aimed at, but a public multi-user product would sit inside it.
   **If this project ever goes multi-user, email them before launch rather than
   after.** This is a product decision, not something the code can resolve.
2. **The event link must be displayed unmodified** for any event we show. The
   admin panel renders `events.ticket_link` / EDMTrain's `link` verbatim; do not
   wrap it in a redirect or a tracking parameter.
3. **Rate limits are set at their discretion and may not be circumvented.** One
   fixed 60 s loop, one key, no parallel fan-out, no second key.

Contrast with RA, where there is no key and no agreement — which is not a
loophole, it just means the only governing document is their site Terms of Use.
That asymmetry is the reason EDMTrain runs the discovery tier: it is the source
we have explicit permission to use.

---

## 11. Phase roadmap

| Phase | Deliverable | Status |
| --- | --- | --- |
| **1** | Rules, this document, `docker-compose`, pgvector schema, observability scaffolding | **done** |
| 2a | Go ingest: EDMTrain discovery adapter, `createdStartDate` cursor, `XADD` | next — needs `EDMTRAIN_API_KEY` |
| 2b | Go ingest: RA availability adapter, fingerprint diff engine, watchlist scheduler, Prometheus metrics | |
| 3 | Python harness: LangGraph FSM, hype scoring, Playwright stage | |
| 4 | Self-healing AST repair loop | |
| 5 | Next.js admin panel over the results stream | |
