# Portfolio launch package

## Recruiter summary

TicketDrop is a multi-user, RA-only onsale-signal product. Users choose a metro
and follow exact artist or venue names. Go detects classified sale-state
changes, Redis Streams decouples ingestion from notification, Python fans out
idempotent email, Postgres keeps the durable audit, and Next.js provides
authenticated user and operator experiences.

## LinkedIn post

I rebuilt TicketDrop as a multi-user Resident Advisor onsale-signal service.

The core constraint is that RA provides no sale webhook, while polling once per
subscriber would multiply third-party traffic. The system now fingerprints the
event fields RA exposes, establishes a silent first-sight baseline, and polls
one shared watch row per unique event. A later classified change flows through
Redis Streams to audited, idempotent email fan-out.

Users select one metro and save direct artist or venue names. The product is
alert-only: it sends the RA and seller links without opening seller pages or
interacting with waiting rooms, CAPTCHAs, payments, or purchase limits.

The production profile fits on one small VPS behind Caddy, with private Redis
and Postgres, backups, health checks, and a labeled replay mode for demos.

Architecture and failure model:
https://github.com/gautamrn/ticketDrop

## Resume bullets

- Built a multi-user RA change-alert system in Go, Python, and Next.js using
  Redis consumer groups and Postgres idempotency for audited email delivery.
- Designed a shared-watch model that scales upstream request volume with unique
  events rather than subscriber count.
- Implemented Auth.js tenancy, direct artist/venue preferences, bounded email
  retry, production containers, health checks, backups, and a fresh-schema
  product smoke test.

Add measured p95/p99 and tested watchlist size only from a dated target-host
report.

## Release checklist

- [ ] Confirm current RA terms permit the intended public polling posture.
- [ ] Scan the complete Git history for secrets and rotate any exposed value.
- [ ] Pass Go, Python, and Next.js CI plus the container product smoke.
- [ ] Build and scan production images.
- [ ] Run the synthetic load report on the target VPS.
- [ ] Configure OAuth callbacks, verified email domain, DNS, and off-host
      backups.
- [ ] Complete and record a database restore drill.
- [ ] Record the flow in `docs/demo-script.md`.
- [ ] Capture landing, events, alert detail, settings, and operator health.
- [ ] Tag `v1.0.0` only after the hosted URL and launch gates pass.

These are launch gates, not implementation claims.
