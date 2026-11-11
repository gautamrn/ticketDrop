# Performance measurement

The executed RA-only benchmark and raw output are in
[`load-test.md`](./load-test.md). Its largest measured phase sustained 39.46
drops/s at 40 source mutations/s with 299.5 ms p50, 580.3 ms p95, and 816.8 ms
p99 source-mutation-to-stream latency on the documented laptop.

The worker-kill result is intentionally not summarized as an unconditional
success. One run had zero missing alerts and five provider-deduplicated
requests; another same-session run had three missing alerts after ten entries
were dead-lettered. The raw outputs and failure hypothesis are preserved in the
report.

## Two latency clocks

TicketDrop reports these separately:

1. **Detection delay:** RA changes until the poll response containing that
   change. With a fixed 5-second interval and uniformly timed changes, scheduler
   delay alone is expected to average about 2.5 seconds, before source and
   network variance.
2. **Stream-to-email latency:** Go publishes the classified change until the
   email provider accepts or rejects the request. This excludes RA time and poll
   delay.

`drop_transitions.latency_ms` records worker queue lag from the Go
`detected_at` timestamp. Delivery attempts, status, and provider outcome are
stored in `notification_deliveries`. A full source-to-email measurement needs a
known mock mutation timestamp; do not infer it from stream timing.

## Required benchmark protocol

Run the Go harness only against the fresh disposable load profile:

```powershell
docker compose -p ticketdrop-load -f docker-compose.load.yml up -d --build
cd ingest-service
go run ./cmd/loadtest
```

A published report must include:

- date, commit, host OS, CPU, memory, Go version, and container limits;
- number of unique events, subscribers per event, messages, and load shape;
- `HARNESS_MAX_CONCURRENCY`, email adapter, and provider behavior;
- wall time, throughput, p50/p95/p99 stream-to-email latency, and sample count;
- missing or duplicate drops and deliveries;
- failed deliveries, Redis memory, Postgres size, and consumer lag;
- whether warm-up samples are included.

The integration smoke proves behavior, not throughput. A log-backed email run
does not predict an external provider's tail latency. Results from a laptop or
WSL must not be presented as production capacity.

## Acceptance checks

Before publishing a result:

- all input messages reach a terminal drop state;
- each `(event_id, fingerprint)` appears once;
- each `(drop_id, user_id, channel)` appears once;
- repeated delivery uses the same provider idempotency key;
- queue lag returns to zero after the burst; and
- the report clearly labels synthetic data and excludes poll delay.
