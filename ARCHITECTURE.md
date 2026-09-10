# TicketDrop — Architecture

Low-latency detection of unannounced house-music ticket drops across Resident
Advisor and EDMTrain, with vector-based hype scoring and a bounded,
self-healing browser assist stage.

---

## 1. Source reality

The blueprint assumed the sources can be polled symmetrically. They cannot.
This is the single most important constraint on the ingestion design.

| Source | Endpoint | Auth | What you actually get |
| --- | --- | --- | --- |
| **Resident Advisor** | `POST https://ra.co/graphql` | None | The site's own public GraphQL API. `eventListings(filters, pageSize, page, sort)` filtered by `areas.eq` (numeric city code) and `listingDate.gte/lte`. Introspection is enabled. Requires a normal browser `User-Agent` and `Referer`. **`Event.tickets` returns `[]` unauthenticated** — see §1.2. Useful area codes: Detroit 19, London 13, Berlin 34. |
| **EDMTrain** | `GET https://edmtrain.com/api/events` | `client=<API key>` query param, free, applied for with an account | Sanctioned listings API for US/Canada. Filter by `locationIds`, `artistIds`, `venueIds`, `startDate`, and critically **`createdStartDate`**. Returns `ticketLink`, lineup with `b2bInd`, venue with coordinates. No images, **no ticket availability**. |

Consequences:

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
| Source | EDMTrain | RA |
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
the ingest service is a change-detector, not a listener.

**Verified against the live RA schema, 2026-08-09.** The original design
fingerprinted per-tier state (`tier_name:tier_state:price`). That is not
possible. `Event.tickets` exists, is queryable without auth, and returns a
`Ticket` type carrying exactly the right fields — `validType`, `priceRetail`,
`onSaleFrom`, `isStoppedSales` — but it **returns an empty array for every
unauthenticated request**. Twelve events sampled across Detroit, London, and
Berlin: no GraphQL errors, twelve empty arrays. The field is filtered, not
forbidden, which is worse because it fails silently rather than loudly.

Every sampled event also reported `ticketingSystem: LEGACY`, meaning RA is not
the seller and links out to a vendor. For those events per-tier data does not
exist on RA at any privilege level.

What an unauthenticated client *can* observe per event:

| Field | Type | Signal |
| --- | --- | --- |
| `isTicketed` | Boolean | tickets exist at all |
| `cost` | StrippedHtml | free text, e.g. "£15 - £20"; a change implies a tier change |
| `presaleStatus` | enum `ACTIVE`/`COMPLETED`/null | a presale opening — the highest-value transition we get |
| `resaleActive` | Boolean | returns going back on sale |
| `queueItEnabled` | Boolean | waiting room, forces alert-only |
| `attending`, `interestedCount` | Int | demand velocity |
| `dateUpdated` | DateTime | *something* changed; useful as a cheap trigger |
| `status` | enum `PENDING`/`LIVE` | listing went live |

So the fingerprint is coarser than planned but still meaningful:

```
fingerprint = sha256(is_ticketed ‖ cost ‖ presale_status ‖ resale_active
                     ‖ queue_it ‖ event_status ‖ date_updated)
```

Detectable transitions, in descending order of value:

- `presaleStatus` null → `ACTIVE` — a presale just opened. Emitted as
  `tier_available`, the closest existing change kind.
- `isTicketed` false → true — tickets now exist (`new_ticketed`).
- `resaleActive` false → true — returns released (`returned_to_sale`).
- `cost` text changes while ticketed — a tier was added or repriced
  (`tier_available`).
- `status` `PENDING` → `LIVE` — the listing itself went public.

`dateUpdated` alone is deliberately **not** a drop. It moves for typo fixes and
flyer swaps, so it participates in the fingerprint but never classifies a change
on its own; a fingerprint delta with no classifying field is recorded and
dropped.

**Consequence for later phases.** True inventory state for a `LEGACY` event
lives on the vendor's page, not on RA. That is not a gap in ingestion, it is the
reason the Playwright stage exists: ingestion detects that *something changed*
cheaply, and the browser stage resolves *what is actually buyable* only for
events that clear the hype threshold. Ingestion is the trigger, not the oracle.

The previous state lives in Redis (`snap:<source>:<event_id>`, TTL 30 days) as the
**whole snapshot**, not just its hash. A hash answers "did something change" but
not "what changed", and classification needs the previous field values to tell a
presale opening from a flyer swap. A snapshot is a few hundred bytes, so the extra
memory buys the only information that makes the delta actionable. Postgres holds
the durable history in `event_snapshots`, written only when the fingerprint moves.

Fingerprint equality is the dedupe primitive that stops a 2-second poll loop from
emitting the same drop 30 times a minute. Two rules matter as much as the hash:

- **The first sighting of an event emits nothing.** It records a baseline. Without
  that, every watchlisted event looks like a fresh drop on startup — the cold-start
  stampede in §9.
- **`attending` and `interestedCount` are excluded from the hash.** They tick
  constantly as people save an event, so including them would change the
  fingerprint on nearly every poll and destroy its value as a cheap "nothing
  happened" filter. They are demand signals for hype scoring, not sale state.

### 1.3 Linking the two tiers

The tiers do not share an id space, so an EDMTrain discovery cannot be polled on
RA without first deciding which RA event is the same night. That match scores
venue similarity (0.45), lineup overlap (0.40), and title similarity (0.15), with
the date as a hard filter rather than a weighted term — two different shows at one
venue with overlapping lineups are common, but the same show on another night is
not the same show.

Candidates are read from our own tables rather than fetched: the RA enumerate loop
has already stored every event in the configured areas, so linking costs one local
query instead of an API call per unresolved event.

The governing bias is that **a false link is worse than no link**. A wrong match
means polling some other event and alerting on a drop the user cannot buy, which
is indistinguishable from a bug and discredits every other alert. A link therefore
requires an absolute score of 0.60 *and* a 0.10 margin over the runner-up;
residencies produce near-identical candidates on the same night at the same venue,
and guessing between them is precisely the error to avoid. Unmatched shows are
reconsidered on the next pass, because a missing candidate now is not evidence
that none exists.

Date tolerance is ±48 h, which is wider than it appears necessary. A date-only
source is stored at midnight UTC while a timed source is stored as a true instant,
so a 22:00 show in Detroit — 02:00 UTC the following day — is already 26 h from its
counterpart before the two sources disagree at all. Widening the window admits
more candidates rather than more false links; the ambiguity margin is what rejects
a wrong pick.

---

## 2. Component diagram

```
   DISCOVERY TIER (~60 s)              AVAILABILITY TIER (~2 s)
  [ EDMTrain REST + key ]              [ RA GraphQL ]
             │                     enumerate + fingerprint diff
             │ createdStartDate              │
             │ delta                         │
             ▼                               ▼
  ┌────────────────────────┐   ┌───────────┐  ┌──────────────────────────┐
  │  Discovery poller      │   │ Resolver  │  │   Availability poller    │
  │  emits 'announced'     │──▶│ event_    │─▶│   bounded worker pool    │
  │  sequential, 1 key     │   │ links     │  │   emits sale changes     │
  └───────────┬────────────┘   └───────────┘  └────────────┬─────────────┘
              │                  venue+lineup+date          │
              │                  local candidates only      │
              └──────────────┬───────────────────────────────┘
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
                        │  results' → Next.js SSE  │
                        └──────────────────────────┘
```

That last edge was originally drawn as a WebSocket. It is Server-Sent Events,
because the traffic is strictly server-to-client: every operator action is a
mutation that wants a response, which is a POST. A bidirectional transport would
buy a channel nothing sends on, and cost a custom server, since the App Router
cannot upgrade a connection inside a route handler. The trade is that `XREAD
BLOCK` holds one Redis connection per connected browser, so the panel scales
with concurrent operators rather than with drop volume.

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

Promotion follows `event_links` onto the RA row when the link already exists.
When it does not — the resolver runs on a ~5 min loop, off the hot path — the
harness still writes the EDMTrain row so the decision survives, then a 30 s
loop (`WATCHLIST_REPOINT_INTERVAL_MS`) rewrites it once the link appears.
Ingest never updates `watchlist`; that table is harness-owned. A false
"we're watching this" is worse than a 30 s delay after the link exists, and
throwing the promotion away because the resolver had not run yet is how the
drop is missed.

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
| `ticket:drops:results` | stream | FSM outcomes, tailed by the admin panel over SSE. `MAXLEN` capped, so it is the live feed and not the record; Postgres is the record. |
| `tdrop-harness` | consumer group on `ticket:drops` | Competing consumers, one per harness worker. |
| `snap:<source>:<event_id>` | string (JSON) | Last availability **snapshot**, TTL 30d. Stores the whole observation, not just its hash, because classification needs the previous field values — see §1.2. |
| `selector_overrides:<domain>` | hash | `logical_name → css_selector`, healed by Claude. |

Message fields on `ticket:drops` (flat, string-valued — Redis streams have no
nested types):

```
drop_id        uuid v7, generated by Go, idempotency key for the whole pipeline
source         "ra" | "edmtrain"
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

**What "degrades" means, precisely.** It depends on *why* a signal is missing,
and conflating the two cases is a live bug rather than a nuance.

A signal that **failed** — an embedding timeout, an event whose artists we have
never seen — contributes zero while keeping its weight in the denominator.
Renormalizing there would let a drop with three dead signals and one strong
artist match score 1.0 and reserve on a quarter of the evidence. Absence of
evidence must not become evidence.

A capability the deployment **cannot produce at all** is different. Nothing
populates `artists.sound_signature`, so the 0.25 sound weight is unearnable by
every event equally. Charging for it does not make scoring conservative; it caps
every possible score at 0.75 and puts the 0.80 threshold permanently out of
reach. This was measured, not predicted: a live drop with a perfect artist and
venue match scored exactly 0.750 and routed to `SKIPPED`, meaning `RESERVING` was
unreachable in any real deployment. A uniform penalty across all events is
arithmetically identical to lowering the threshold, except that it is invisible
and nobody chose it.

So a globally-absent capability is excluded from the denominator at startup,
detected from the data actually present rather than from a config flag, and
logged when it happens. A signal that merely failed for one drop is not. The
first is a statement about the deployment; the second is about the event.

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
3. Candidates are validated in-page (exactly one match, visible, enabled) before
   use, taking the first that passes, up to three. An unvalidated selector is
   never executed. Ranking beyond the top guess costs only local DOM checks —
   there is still exactly one model call — and a model that gets the right
   answer second should not fail the drop.
4. On validation success, write through to `selector_overrides:<domain>` in Redis
   **and** the `selector_overrides` table, then complete the reservation and
   terminate in `NOTIFIED`.
5. On failure, transition to `FAILED` and raise a manual alert. **One attempt.**

`SELF_HEAL` terminates rather than re-entering `RESERVING`. An earlier draft of
this document said "resume `RESERVING` once", which contradicted the state
diagram in §4 and would have made the graph cyclic. The one-attempt cap is then
only as reliable as a counter that every path remembers to increment. Keeping the
graph acyclic makes the cap a property of the topology instead — there is no edge
back, so there is no second attempt to guard against.

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
| `event_snapshots` | Append-only event-level observations used to audit availability changes. |
| `drops` | One row per detected drop = the FSM instance, including `state` and `hype_score`. |
| `drop_transitions` | Append-only FSM audit log with per-stage `latency_ms`. |
| `user_preferences` | The vectors hype scoring matches against. |
| `selector_overrides` | Durable copy of healed selectors; Redis is the cache. |

`events` and `event_snapshots` keep a `raw jsonb` column. When an upstream schema
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

## 10. Operating posture

Automated ticket *purchasing* sits in a regulated area: the US **BOTS Act**
(2016) and the UK's **Breaching of Limits on Ticket Sales Regulations 2018**
prohibit circumventing security measures, CAPTCHAs, waiting rooms, or per-person
purchase limits on ticket sellers. RA surfaces `queueItEnabled` precisely because
some events sit behind a Queue-it waiting room.

The reservation stage is therefore governed by `RESERVATION_MODE`:

| Mode | Behavior |
| --- | --- |
| `alert` *(default)* | No browser purchase path. Detect, score, notify with a deep link. Fastest to build, zero legal exposure. |
| `assist` | Launch a **headed** browser, navigate to the event, locate the ticket CTA, then hand the live session to the human for CAPTCHA, queue, and payment. The bot never completes a transaction. |

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

## 11. Implementation status

| Phase | Deliverable | Status |
| --- | --- | --- |
| **1** | Rules, this document, `docker-compose`, pgvector schema, Prometheus config | **done** |
| **2a** | Go ingest: EDMTrain discovery adapter, `createdStartDate` cursor, `XADD`, Prometheus metrics | **done** — mock server included; live run needs `EDMTRAIN_API_KEY` |
| **2b** | Go ingest: RA availability adapter, fingerprint diff engine, watchlist scheduler, bounded worker pool, cross-source resolver | **done** — verified against `cmd/mock-ra`; live run needs only `RA_AREAS` |
| **3** | Python harness: LangGraph FSM, hype scoring, Playwright stage | **done** — verified Go→Redis→harness end to end; `assist` navigates a real browser |
| **4** | Self-healing AST repair loop | **done** — verified against a fake LLM; live Claude behavior unverified (no key) |
| **5** | Next.js admin panel over the results stream | **done** — SSE tail, FSM detail, health strip, four overrides; verified against live Redis and Postgres |

### Known gaps

- **The harness exposes no Prometheus endpoint.** Its measured stage latency is
  stored durably in `drop_transitions`; the Go service exposes Prometheus
  metrics for ingestion.
- **`artists.sound_signature` is never populated.** It needs audio analysis
  (BPM/key extraction) that no adapter performs, so the 0.25 sound weight is
  currently unearnable and is excluded from the denominator at runtime — see §6.
- **The `assist` path navigates but has never reached a sale page.** Chromium is
  installed and the stage now launches it, resolves the URL, and classifies the
  HTTP status for real — a `RESERVING` attempt against `ra.co` returns 403,
  which is a correct outcome rather than a defect, since RA refuses headless
  clients. What remains unproven is everything downstream of a *successful*
  navigation: selector resolution against real DOM, and therefore a self-heal
  provoked by an actual selector miss rather than an injected one.
- **Neither real API client has been called.** `VoyageEmbedder` and
  `ClaudeClient` are tested against mock transports only.
- **The panel has no authentication.** It exposes four mutating endpoints and
  binds to localhost. That is adequate for a single operator on one machine and
  is not adequate for anything reachable from a network; deploying it anywhere
  else needs an auth layer in front, not a change to the panel.
- **The panel's SSE tail holds one Redis connection per connected browser.**
  Fine for one operator, wrong past roughly a dozen — see `admin-panel/README.md`.
