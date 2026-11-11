# TicketDrop architecture

TicketDrop is an RA-only onsale-signal product. A user chooses one configured
Resident Advisor metro, follows exact artist or venue names, and may opt into
an in-app discovery feed based on exact RA genres and an optional listed-price
ceiling. The system enumerates that metro, polls each subscribed event once
through a shared watchlist, and emails subscribers only after a classified
sale-state change.

## Source limits and change detection

RA exposes a public GraphQL catalogue but no drop webhook or versioned partner
contract. For sampled events, `ticketingSystem` is `LEGACY` and unauthenticated
`Event.tickets` is empty. TicketDrop therefore cannot observe inventory or
per-tier availability. It can observe:

- `presaleStatus` null → `ACTIVE` (`tier_available`);
- `isTicketed` false → true (`new_ticketed`);
- `resaleActive` false → true (`returned_to_sale`);
- normalized `cost` changing while ticketed (`tier_available`).

The fingerprint is SHA-256 over `isTicketed`, normalized `cost`,
`presaleStatus`, `resaleActive`, `queueItEnabled`, event `status`, and
`dateUpdated`. `dateUpdated` makes unknown upstream edits visible but never
classifies a change by itself. `attending` and `interestedCount` are excluded
because they move frequently without indicating a sale-state change.

The first observation writes a Redis baseline and a durable event snapshot but
emits nothing. Without a silent first sight, a cold start or lost cache would
report every watched event as a new onsale signal. A changed fingerprint that
the classifier cannot explain is stored for audit and is not published.

## Components and ownership

```text
RA GraphQL
  │ enumerate catalogue + poll shared watchlist
  ▼
Go ingest ── snapshot + outbox ──▶ Postgres
  │                                  │ stable drop ID
  │ catalogue                        ▼
  └──────────────────────▶ Redis Stream ticket:drops
                                      │ consumer group
                                      ▼
                                  Python worker
                                         │ drops, transitions, deliveries
                                         │ idempotent email
                                         ▼
                                      Postgres ◀── Next.js read model and user APIs
```

- Go owns outbound RA calls, catalogue writes, snapshots, fingerprinting, the
  `ingest_outbox`, and publishing to `ticket:drops`.
- Python owns the scout, subscription reconciliation, stream consumption,
  `watchlist`, drops, transitions, and email deliveries. It never polls RA.
- Next.js owns authenticated settings, direct artist/venue preference writes,
  feed-only discovery matching, explicit Watch/Unwatch, personal event and
  alert views, and the operator read model.
- Postgres is the durable system of record. Redis contains the stream, poll
  snapshots (`snap:ra:<event_id>`), and short-lived claims.

The scout matches normalized full artist or venue names for users in the same
metro. It creates at most 25 active subscriptions per user and never reactivates
a row marked `user_disabled`. Reconciliation projects all active subscriptions
onto one `watchlist` row per RA event. Ten subscribers therefore add fan-out
work, not ten upstream polls.

The catalogue is normally Go-owned. If a valid stream message refers to a
missing event, Python may upsert a marked stub so the drop audit is not lost;
the next Go catalogue upsert corrects it.

## Discovery semantics

Discovery is a Postgres read model evaluated by the Next.js panel; it does not
create subscriptions, watchlist rows, or email deliveries. A discovery becomes
alertable only after the user explicitly selects Watch. This prevents a broad
genre preference from multiplying upstream polls or producing an email storm.

Genre matching is case-normalized equality against RA's catalogue labels, not
fuzzy or model-generated matching. RA exposes cost as free-form text. The panel
therefore recognizes only explicit USD, GBP, EUR, and free listings; when a
price ceiling is configured, unknown or differently denominated costs are
excluded rather than guessed. The query first bounds candidates to 200 upcoming
events in the user's metro before parsing costs in the application.

## Delivery and idempotency

The ingest service commits a classified snapshot and `ingest_outbox` row in one
Postgres transaction. Its dispatcher leases due rows and publishes a stable
drop ID to Redis. A crash after `XADD` but before recording `published_at`
therefore republishes the same ID instead of losing the source transition.

Redis consumer groups provide at-least-once delivery: a worker can fail after a
side effect but before `XACK`, so exactly-once processing is not available
across Postgres, Redis, and the email provider.

Correctness comes from layered idempotency:

- `drops` is unique on `(event_id, fingerprint)`;
- `notification_deliveries` is unique on `(drop_id, user_id, channel)`;
- every retry uses `Idempotency-Key: drop-<drop_id>-user-<user_id>-email`;
- `SET drop:claim:<drop_id> NX EX 900` reduces concurrent duplicate work;
- entries idle for 60 seconds are reclaimed and poison entries are
  dead-lettered after three deliveries.

The Redis claim is an optimization, not the durable correctness boundary.
Redis loss can repeat work; Postgres uniqueness and provider idempotency prevent
a second drop row or intentional second email.

`XAUTOCLAIM` increments a delivery count even while a dead worker's short-lived
claim remains live. The worker therefore checks the claim and durable terminal
state before applying the delivery ceiling. A still-claimed entry remains
pending until the claim expires; a terminal drop is acknowledged; only a
claimed, nonterminal poison entry is dead-lettered.

## Latency

Production starts with a 5-second RA poll interval, six workers, and one metro.
The expected scheduling delay is about half the interval before source and
network variance, so polling dominates detection time. The Go diff and stream
append happen after the changed response arrives.

Stream-to-email latency is measured separately from poll delay. The worker
records queue lag in `drop_transitions.latency_ms`; provider response time and
delivery status remain in `notification_deliveries`. Do not combine these into
an end-to-end source percentile unless the source-change time is known.

## Alert-only boundary

TicketDrop sends information and unmodified links. It does not open seller
pages, enter waiting rooms, solve CAPTCHAs, submit payments, or evade account or
purchase limits. `queueItEnabled` adds a warning to the email but does not
suppress it. This boundary avoids conduct regulated by the US BOTS Act and the
UK Breaching of Limits on Ticket Sales Regulations 2018, and keeps unknown
seller terms outside the system.

## Failure model

| Failure | Consequence | Mitigation |
| --- | --- | --- |
| RA schema drift | Parsing or classification can fail or silently lose a field | Preserve upstream payloads in `raw jsonb`, alert on parse/unclassified-change rates, and backfill after adapting the parser |
| IP block | One egress IP loses live catalogue and polling | Keep the interval configurable and conservative, use few bounded workers and one metro initially, and back off rather than adding identities |
| Poison message | One malformed entry can repeatedly return to the pending list | Validate at intake, reclaim idle entries, record `FAILED` when a usable drop id exists, and acknowledge after three deliveries |
| Redis loss | Stream entries, poll baselines, and claims can disappear; rows already marked published are not replayed automatically | Postgres snapshots and outbox records remain available for an operator replay; stable drop IDs plus database/provider idempotency make that replay safe. Redis AOF reduces but does not eliminate this failure |
| Crash around stream publish | Publishing before durable state can lose the change; crashing after publish can duplicate it | Snapshot and outbox commit atomically; a leased row is marked only after `XADD`, and retries reuse its drop ID |
| Duplicate subscribers | Fan-out or upstream requests could multiply | One subscription per `(user_id, event_id)`, one shared watch row per event, and one delivery per `(drop_id, user_id, channel)` |

## Deployment boundary

The production profile is one VPS with Caddy as the only public ingress and
private Go, Python, Next.js, Postgres, and Redis services. This is a cost
boundary, not high availability. Secrets come only from environment variables,
all blocking calls require timeouts, and public live polling remains contingent
on RA's current terms; replay mode is the fallback.
