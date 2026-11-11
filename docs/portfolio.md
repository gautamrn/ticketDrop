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
provider key on retries.

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
- Performance results separate poll delay from stream-to-email latency.

## Known limits

- RA's GraphQL schema is public but undocumented and may drift.
- One VPS and one egress IP are cost boundaries, not high availability.
- Exact full-name matching misses aliases but avoids false watches.
- Public live polling is contingent on current RA terms; replay mode is the
  fallback.

## Resume bullets

- Built a multi-user RA onsale-signal system in Go, Python, and Next.js with a
  shared watchlist that keeps source load proportional to unique events.
- Designed at-least-once Redis processing with Postgres and email-provider
  idempotency to prevent duplicate alerts after worker failure.
- Implemented authenticated direct artist/venue preferences, audited email
  fan-out, bounded retries, production containers, and contract-faithful smoke
  tests.

Add tested throughput and latency percentiles only from a dated report whose
workload and measurement boundaries are stated.
