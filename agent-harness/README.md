# agent-harness

Python 3.12+ service that converts user preferences into shared RA watches,
consumes `ticket:drops`, records durable outcomes, and fans idempotent email
alerts out to active subscribers.

It never calls RA. The Go ingester is the only upstream poller and stream
publisher.

## Responsibilities

- The scout runs every 60 seconds and matches normalized full artist or venue
  names against catalogue events in each user's selected metro.
- The subscription reconciler runs every 15 seconds and projects active
  `event_subscriptions` onto one shared `watchlist` row per event.
- The worker consumes the `tdrop-harness` group, reclaims idle messages, writes
  `drops` and `drop_transitions`, and sends subscriber email.
- The notifier records one `notification_deliveries` row per drop, user, and
  channel and retries transient provider failures with one stable idempotency
  key.

The scout does exact normalized full-name matching. It does not guess aliases.
It will match `Prospa` to `prospa`, but not to `Prospa (Live)`. Users are capped
at 25 active subscriptions. Explicit Watch uses the same cap; Unwatch sets
`user_disabled`, which the scout must not reverse.

## Layout

```text
src/harness/app.py             composition and background tasks
src/harness/config.py          environment-derived settings
src/harness/scout.py           preference matching and watch reconciliation
src/harness/bus.py             consumer group, reclaim, claim, and ack
src/harness/messages.py        flat stream-message validation
src/harness/store.py           Postgres access and ownership boundaries
src/harness/worker.py          bounded at-least-once processing
src/harness/notify/email.py    Resend/log clients and delivery idempotency
src/harness/health.py          liveness/readiness HTTP endpoint
```

## Stream processing

The worker:

1. reads new entries and reclaims entries idle for 60 seconds;
2. dead-letters malformed or three-times-delivered entries;
3. takes `SET drop:claim:<drop_id> NX EX 900`;
4. ensures the event exists and inserts the drop;
5. records queue lag and classification detail in one transition;
6. loads active subscribers with verified email channels;
7. sends concurrently within `HARNESS_MAX_CONCURRENCY`;
8. marks the drop `NOTIFIED` after fan-out, or `SKIPPED` when no subscriber
   exists; and
9. acknowledges only after a terminal outcome.

Redis delivery is at least once. The claim reduces overlapping work but cannot
make an external side effect atomic with `XACK`. Durable protection is:

- unique `(event_id, fingerprint)` on `drops`;
- unique `(drop_id, user_id, channel)` on deliveries;
- the same `drop-<drop_id>-user-<user_id>-email` provider key on every retry.

This does not currently guarantee application-level completion. The executed
worker-kill benchmark found that reclaim can raise the delivery count to the
dead-letter ceiling before a stale claim expires; see
[`docs/load-test.md`](../docs/load-test.md).

One subscriber failure is isolated to that delivery row and does not suppress
other subscribers.

## Run

```powershell
cd agent-harness
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m harness run
```

`DATABASE_URL` is required. Important settings include `REDIS_URL`,
`REDIS_STREAM`, `HARNESS_MAX_CONCURRENCY`, reclaim and delivery limits,
`SCOUT_INTERVAL_MS`, `SUBSCRIPTION_RECONCILE_INTERVAL_MS`, `RESEND_API_KEY`,
`ALERT_FROM_EMAIL`, `APP_BASE_URL`, and `UNSUBSCRIBE_SECRET`.

With no `RESEND_API_KEY`, the log client is used. Production requires Resend
and a non-development unsubscribe secret. The health server listens on port
2113 by default and checks both Postgres and Redis for readiness.

## Tests

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\pytest.exe -q
```

The suite covers message validation, claim/redelivery behavior, duplicate
fingerprints, poison-message handling, scout matching, the `user_disabled`
invariant, notification retries, and duplicate-email prevention.
