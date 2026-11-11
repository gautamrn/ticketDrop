# TicketDrop case study

## Problem

Resident Advisor has no onsale webhook, and its unauthenticated ticket array is
empty for sampled events. Polling once per user would multiply source traffic,
while treating every listing edit as a sale signal would train users to ignore
alerts.

## Design

The Go service enumerates one configured RA metro and polls only a shared
watchlist. It fingerprints the event-level sale fields RA actually exposes.
First sight establishes a baseline; only a later, classified delta reaches
Redis Stream `ticket:drops`.

The Python service matches users' exact artist or venue names to catalogue
events, reconciles all active subscriptions into one watch row per event, and
fans each classified change out by email. Next.js provides authenticated
settings, catalogue, alerts, and operator views. Postgres stores the durable
catalogue, decisions, transitions, subscriptions, and deliveries.

## Decisions worth defending

### Shared polling

Ten users following the same show create ten subscription rows but one polling
row. Source request volume follows unique watched events rather than account
growth. The cost is a reconciliation loop and a small delay before a new
subscription reaches the poller.

### At-least-once delivery

Redis consumer groups recover work after a process failure, but cannot atomically
commit Postgres and email with `XACK`. TicketDrop accepts redelivery and makes
side effects idempotent with unique drop and delivery keys plus a stable
provider key on retries. The transport is at least once; the application cannot
yet make the same unconditional claim because a measured reclaim/dead-letter
race lost alerts under one worker-kill timing.

### Coarse but honest signals

The fingerprint covers ticketed state, normalized cost, presale state, resale
state, queue state, listing status, and update time. Demand counters are
excluded. An update timestamp can trigger comparison but cannot classify an
alert by itself.

### Alert-only operation

TicketDrop sends RA and seller links but does not open seller pages, enter a
queue, handle payment, or bypass purchase controls. This is both a legal safety
boundary and a scope boundary.

## Verification

- Go uses table-driven fingerprint and pipeline tests with network doubles.
- Python tests stream recovery, duplicate suppression, matching, fan-out, and
  notification retries.
- Next.js lint, type checking, and production build run in CI.
- The container product smoke starts with a fresh schema and verifies silent
  baseline, automatic subscription, one classified drop, and one email.
- The repeatable load report includes raw output, increasing source rates, and
  repeated worker-kill evidence.

## Known limits

- RA's GraphQL schema is public but undocumented and may drift.
- One VPS and one egress IP are cost boundaries, not high availability.
- Exact full-name matching misses aliases but avoids false watches.
- Public live polling is contingent on current RA terms; replay mode is the
  fallback.

## Resume bullets

- Built a multi-user RA onsale-signal system in Go, Python, and Next.js with a
  shared watchlist that keeps source load proportional to unique events.
- Sustained 39.46 classified drops/s at a 40 events/s source rate on a
  documented laptop run, with 299.5 ms p50 and 816.8 ms p99
  mutation-to-stream latency.
- Prevented duplicate accepted alerts in repeated 600-event worker-kill runs
  with Postgres and provider idempotency; also identified a reclaim/dead-letter
  race that caused three missing alerts in one run.
- Implemented authenticated direct artist/venue preferences, audited email
  fan-out, bounded retries, production containers, and contract-faithful smoke
  tests.

Source and measurement boundaries:
[`docs/load-test.md`](./load-test.md).
