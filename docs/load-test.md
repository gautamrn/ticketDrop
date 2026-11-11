# Load test

Generated from an executed disposable-stack run at commit `2dbc6cb7d5fefb2309be727ed0c3ced3091f73a8 (dirty load-test implementation)`.

## Harness choice

A Go harness fits better than k6 because this benchmark must correlate mock-source timestamps with Redis Stream IDs and durable Postgres rows, not just generate HTTP traffic. It reuses the repository's existing pgx and go-redis dependencies and adds no load-test runtime to the deployment.

## Scope and clock

The source clock is the mock RA `changed_at` timestamp returned when a ticket-state mutation is accepted; the end clock is the matching Redis Stream entry ID timestamp. New catalogue rows are not timed because the first observation is intentionally a silent baseline. `mock-edmtrain` is absent from this RA-only commit, so reviving it would measure a deleted ingestion path.

## Executed environment

- OS/architecture: `windows/amd64`
- CPU: `13th Gen Intel(R) Core(TM) i7-1355U` (12 logical CPUs)
- Physical memory: `34032574464` bytes
- Go: `go1.25.6`; Docker Engine: `29.1.3`
- Container limits: `none`
- Measured source window: `2026-11-11T05:04:48.052Z` to `2026-11-11T05:08:18.648Z`
- Command, run from `ingest-service/`:

```powershell
go run ./cmd/loadtest -repo .. -rates 2,5,10,20,40 -duration 30s -kill-rate 20 -kill-duration 30s -drain-timeout 120s -report ../docs/load-test.md -raw-output ../docs/load-test-raw.txt
```

The images were built earlier in this session, then the reported run was reset and started from the repository root with these exact commands:

```powershell
docker compose -p ticketdrop-load -f docker-compose.load.yml down -v --remove-orphans
docker compose -p ticketdrop-load -f docker-compose.load.yml up -d
```

For a first run from a clean checkout, add `--build` to the final command.

## Results

| Phase | Target events/s | Events | Sustained drops/s | p50 ms | p95 ms | p99 ms | Duplicate stream entries | Duplicate alerts | Missing alerts | Shed poll jobs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rate-02 | 2 | 60 | 2.02 | 265.2 | 340.1 | 360.4 | 0 | 0 | 0 | 0 |
| rate-05 | 5 | 150 | 5.01 | 287.1 | 531.8 | 565.2 | 0 | 0 | 0 | 0 |
| rate-10 | 10 | 300 | 10.01 | 300.4 | 537.8 | 583.0 | 0 | 0 | 0 | 0 |
| rate-20 | 20 | 600 | 19.72 | 284.9 | 541.4 | 599.8 | 0 | 0 | 0 | 0 |
| rate-40 | 40 | 1200 | 39.46 | 299.5 | 580.3 | 816.8 | 0 | 0 | 0 | 0 |
| worker-kill | 20 | 600 | 19.78 | 292.0 | 533.1 | 588.0 | 0 | 0 | 0 | 0 |

“Sustained drops/s” is unique matching stream emissions divided by first-source-mutation to last-emission wall time, so drain time is included. A duplicate alert means the idempotent mock provider accepted more than one alert for an expected source event; repeated HTTP attempts with the same idempotency key are counted separately in the raw output.

## Worker-kill result

The harness was sent `SIGKILL` 15.001 seconds into the worker-kill phase and Docker restarted it in 3211 ms. After drain there were 0 pending stream entries, 600 durable drops (600 `NOTIFIED`, 0 `FAILED`), 600 durable delivery rows (600 `sent`, 0 still `sending`), 5 provider-deduplicated requests, 0 duplicate accepted alerts, and 0 missing alerts.

This final run preserved all alerts, but an earlier same-session run of the same 20 events/s kill profile did not: it accepted 597 of 600 alerts, leaving 3 missing with 0 duplicate accepted alerts. Its post-run query found 10 drops failed by `delivery limit exceeded`: 6 had a `sent` delivery and 4 remained `sending` (590 other drops were `NOTIFIED` with `sent` delivery). The zero-duplicate assertion held in both runs, but application-level at-least-once delivery is therefore not reliable under this kill timing.

The failure mechanism is supported by the code and run configuration: reclaim runs after 1 second, the claim TTL is 5 seconds, and the delivery ceiling is 3; `Worker.handle` checks the delivery ceiling before checking whether another worker's claim is still live. Depending on kill timing, reclaimed entries can reach the ceiling and be dead-lettered before the stale claim expires.

## Bottleneck evidence

The worst observed p99 was 816.8 ms in `rate-40`; that phase sustained 39.46 drops/s and shed 0 poll jobs. With no shedding at the highest executed rate, this run did not locate the throughput ceiling. The p50/p95 shape is consistent with the configured 500 ms poll scheduler being the dominant latency floor; a higher-rate run is required before attributing saturation to Redis, Postgres, or the email fan-out.

These laptop/mock-provider results are not production capacity and do not include real RA or Resend network latency.

## Raw output

The final run is stored in [`load-test-raw.txt`](./load-test-raw.txt).

```text
RUN_METADATA {"arch":"amd64","command":"go run ./cmd/loadtest -repo .. -rates 2,5,10,20,40 -duration 30s -kill-rate 20 -kill-duration 30s -drain-timeout 120s -report ../docs/load-test.md -raw-output ../docs/load-test-raw.txt","commit":"2dbc6cb7d5fefb2309be727ed0c3ced3091f73a8 (dirty load-test implementation)","container_limits":"none","cpu":"13th Gen Intel(R) Core(TM) i7-1355U","docker":"29.1.3","go":"go1.25.6","logical_cpus":"12","memory_bytes":"34032574464","os":"windows"}
PHASE_START name=rate-02 rate=2 duration=30s expected=60 kill=false
PHASE_RESULT {"name":"rate-02","target_rate_eps":2,"duration_seconds":30,"expected_events":60,"achieved_input_eps":2.0336184929673573,"sustained_output_eps":2.017444228555227,"latency_p50_ms":265.225,"latency_p95_ms":340.103,"latency_p99_ms":360.374,"unique_emissions":60,"duplicate_emissions":0,"drop_rows":60,"notified_drops":60,"failed_drops":0,"delivery_limit_failures":0,"delivery_rows":60,"sent_deliveries":60,"sending_deliveries":0,"accepted_alerts":60,"provider_deduplicated_requests":0,"duplicate_alerts":0,"missing_alerts":0,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":false,"first_mutation_unix_ms":1794373488052,"last_emission_unix_ms":1794373517793,"mutation_failures":0,"terminal_wait_seconds":0.4546891,"baseline_wait_seconds":0.7424553,"source_create_wait_seconds":0.8440826}
PHASE_START name=rate-05 rate=5 duration=30s expected=150 kill=false
PHASE_RESULT {"name":"rate-05","target_rate_eps":5,"duration_seconds":30,"expected_events":150,"achieved_input_eps":5.033058385742151,"sustained_output_eps":5.009544003392634,"latency_p50_ms":287.068,"latency_p95_ms":531.814,"latency_p99_ms":565.167,"unique_emissions":150,"duplicate_emissions":0,"drop_rows":150,"notified_drops":150,"failed_drops":0,"delivery_limit_failures":0,"delivery_rows":150,"sent_deliveries":150,"sending_deliveries":0,"accepted_alerts":150,"provider_deduplicated_requests":0,"duplicate_alerts":0,"missing_alerts":0,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":false,"first_mutation_unix_ms":1794373520407,"last_emission_unix_ms":1794373550350,"mutation_failures":0,"terminal_wait_seconds":0.1248948,"baseline_wait_seconds":0.1062774,"source_create_wait_seconds":1.1527379}
PHASE_START name=rate-10 rate=10 duration=30s expected=300 kill=false
PHASE_RESULT {"name":"rate-10","target_rate_eps":10,"duration_seconds":30,"expected_events":300,"achieved_input_eps":10.031216879429667,"sustained_output_eps":10.007007127545565,"latency_p50_ms":300.448,"latency_p95_ms":537.841,"latency_p99_ms":583.039,"unique_emissions":300,"duplicate_emissions":0,"drop_rows":300,"notified_drops":300,"failed_drops":0,"delivery_limit_failures":0,"delivery_rows":300,"sent_deliveries":300,"sending_deliveries":0,"accepted_alerts":300,"provider_deduplicated_requests":0,"duplicate_alerts":0,"missing_alerts":0,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":false,"first_mutation_unix_ms":1794373553994,"last_emission_unix_ms":1794373583973,"mutation_failures":0,"terminal_wait_seconds":0.2438093,"baseline_wait_seconds":0.4193606,"source_create_wait_seconds":1.9778780999999999}
PHASE_START name=rate-20 rate=20 duration=30s expected=600 kill=false
PHASE_RESULT {"name":"rate-20","target_rate_eps":20,"duration_seconds":30,"expected_events":600,"achieved_input_eps":20.031234503649195,"sustained_output_eps":19.716140741834323,"latency_p50_ms":284.908,"latency_p95_ms":541.363,"latency_p99_ms":599.796,"unique_emissions":600,"duplicate_emissions":0,"drop_rows":600,"notified_drops":600,"failed_drops":0,"delivery_limit_failures":0,"delivery_rows":600,"sent_deliveries":600,"sending_deliveries":0,"accepted_alerts":600,"provider_deduplicated_requests":0,"duplicate_alerts":0,"missing_alerts":0,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":false,"first_mutation_unix_ms":1794373590128,"last_emission_unix_ms":1794373620560,"mutation_failures":0,"terminal_wait_seconds":0.6080299,"baseline_wait_seconds":0.6372831,"source_create_wait_seconds":4.2326467}
PHASE_START name=rate-40 rate=40 duration=30s expected=1200 kill=false
PHASE_RESULT {"name":"rate-40","target_rate_eps":40,"duration_seconds":30,"expected_events":1200,"achieved_input_eps":40.02388558785407,"sustained_output_eps":39.45872866313322,"latency_p50_ms":299.485,"latency_p95_ms":580.338,"latency_p99_ms":816.755,"unique_emissions":1200,"duplicate_emissions":0,"drop_rows":1200,"notified_drops":1200,"failed_drops":0,"delivery_limit_failures":0,"delivery_rows":1200,"sent_deliveries":1200,"sending_deliveries":0,"accepted_alerts":1200,"provider_deduplicated_requests":0,"duplicate_alerts":0,"missing_alerts":0,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":false,"first_mutation_unix_ms":1794373628796,"last_emission_unix_ms":1794373659208,"mutation_failures":0,"terminal_wait_seconds":0.5493257,"baseline_wait_seconds":0.8700437,"source_create_wait_seconds":6.0068128}
PHASE_START name=worker-kill rate=20 duration=30s expected=600 kill=true
WORKER_RESTART phase=worker-kill at_seconds=15.001 restart_ms=3211
PHASE_RESULT {"name":"worker-kill","target_rate_eps":20,"duration_seconds":30,"expected_events":600,"achieved_input_eps":20.031495787913947,"sustained_output_eps":19.779799988090623,"latency_p50_ms":292.03,"latency_p95_ms":533.121,"latency_p99_ms":587.992,"unique_emissions":600,"duplicate_emissions":0,"drop_rows":600,"notified_drops":600,"failed_drops":0,"delivery_limit_failures":0,"delivery_rows":600,"sent_deliveries":600,"sending_deliveries":0,"accepted_alerts":600,"provider_deduplicated_requests":5,"duplicate_alerts":0,"missing_alerts":0,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":true,"worker_kill_at_seconds":15.000685,"worker_restart_ms":3211,"first_mutation_unix_ms":1794373668314,"last_emission_unix_ms":1794373698648,"mutation_failures":0,"terminal_wait_seconds":0.482299,"baseline_wait_seconds":0.4471852,"source_create_wait_seconds":6.8687416}
```

The same-session worker-kill failure and its post-run SQL counts are stored in [`load-test-worker-kill-failure-raw.txt`](./load-test-worker-kill-failure-raw.txt).

```text
RUN_METADATA {"arch":"amd64","command":"go run ./cmd/loadtest -repo .. -rates 2,5,10,20,40 -duration 30s -kill-rate 20 -kill-duration 30s -drain-timeout 120s -report ../docs/load-test.md -raw-output ../docs/load-test-raw.txt","commit":"2dbc6cb7d5fefb2309be727ed0c3ced3091f73a8 (dirty load-test implementation)","container_limits":"none","cpu":"13th Gen Intel(R) Core(TM) i7-1355U","docker":"29.1.3","go":"go1.25.6","logical_cpus":"12","memory_bytes":"34032574464","os":"windows"}
PHASE_START name=worker-kill rate=20 duration=30s expected=600 kill=true
WORKER_RESTART phase=worker-kill at_seconds=15.000 restart_ms=3280
PHASE_RESULT {"name":"worker-kill","target_rate_eps":20,"duration_seconds":30,"expected_events":600,"achieved_input_eps":20.030833996927043,"sustained_output_eps":19.745700667668416,"latency_p50_ms":288.17,"latency_p95_ms":531.915,"latency_p99_ms":577.832,"unique_emissions":600,"duplicate_emissions":0,"drop_rows":600,"delivery_rows":600,"accepted_alerts":597,"provider_deduplicated_requests":0,"duplicate_alerts":0,"missing_alerts":3,"poll_jobs_dropped":0,"pending_after_drain":0,"worker_killed":true,"worker_kill_at_seconds":15.0001486,"worker_restart_ms":3280,"first_mutation_unix_ms":1794373261097,"last_emission_unix_ms":1794373291484,"mutation_failures":0,"terminal_wait_seconds":0.6029179,"baseline_wait_seconds":0.422865,"source_create_wait_seconds":3.2222211}
POST_RUN_QUERY {"notified_sent":590,"failed_sending":4,"failed_sent":6,"delivery_limit_failed":10}
```
