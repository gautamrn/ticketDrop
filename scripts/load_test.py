"""Publish synthetic drops into a running isolated stack and report measured latency.

This is not a source-network benchmark: it starts at Redis XADD, after the
poll response has already been classified. Run it only against a disposable
database because it creates clearly labeled benchmark rows.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import redis.asyncio as redis


async def run(count: int, output: Path) -> None:
    database_url = os.environ["DATABASE_URL"]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    pool = await asyncpg.create_pool(database_url)
    client = redis.from_url(redis_url, decode_responses=True)
    event_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    drop_ids = [str(uuid.uuid4()) for _ in range(count)]

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO users (id, email, name) VALUES ($1::uuid, $2, 'Benchmark user')",
            user_id,
            f"benchmark-{user_id}@local.invalid",
        )
        await conn.execute(
            """
            INSERT INTO events (
                id, source, source_event_id, title, content_url, starts_at, raw
            ) VALUES (
                $1::uuid, 'ra', $2, '[Benchmark] Synthetic event',
                'https://ra.co/events', now() + interval '7 days', '{"benchmark":true}'
            )
            """,
            event_id,
            f"benchmark-{event_id}",
        )
        await conn.execute(
            """
            INSERT INTO event_subscriptions (user_id, event_id, origin)
            VALUES ($1::uuid, $2::uuid, 'explicit')
            """,
            user_id,
            event_id,
        )
        await conn.execute(
            """
            INSERT INTO notification_channels (
                id, user_id, kind, destination, verified_at
            ) VALUES ($1::uuid, $2::uuid, 'email', $3, now())
            """,
            channel_id,
            user_id,
            f"benchmark-{user_id}@local.invalid",
        )

    started = time.perf_counter()
    detected: dict[str, datetime] = {}
    for index, drop_id in enumerate(drop_ids):
        now = datetime.now(UTC)
        detected[drop_id] = now
        await client.xadd(
            "ticket:drops",
            {
                "drop_id": drop_id,
                "source": "ra",
                "event_id": f"benchmark-{event_id}",
                "event_url": "https://ra.co/events",
                "ticket_link": "https://ra.co/events",
                "title": "[Benchmark] Synthetic event",
                "venue": "Benchmark venue",
                "starts_at": (now.replace(microsecond=0)).isoformat(),
                "artists": "Benchmark artist",
                "attending": "100",
                "queue_it": "false",
                "change": "tier_available",
                "fingerprint": f"benchmark-{index}-{uuid.uuid4()}",
                "detected_at": now.isoformat(),
                "raw": '{"benchmark":true}',
            },
        )

    deadline = time.monotonic() + 120
    completed = 0
    while time.monotonic() < deadline:
        async with pool.acquire() as conn:
            completed = int(
                await conn.fetchval(
                    """
                    SELECT count(*) FROM drops
                     WHERE id = ANY($1::uuid[]) AND state IS NOT NULL
                    """,
                    drop_ids,
                )
                or 0
            )
        if completed == count:
            break
        await asyncio.sleep(0.25)
    if completed != count:
        raise RuntimeError(f"timed out: {completed}/{count} drops completed")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id::text,
                   extract(epoch FROM (max(t.at) - d.detected_at)) * 1000 AS latency_ms
              FROM drops d
              JOIN drop_transitions t ON t.drop_id = d.id
             WHERE d.id = ANY($1::uuid[])
             GROUP BY d.id, d.detected_at
            """,
            drop_ids,
        )
        delivery_count = int(
            await conn.fetchval(
                "SELECT count(*) FROM notification_deliveries WHERE user_id=$1::uuid",
                user_id,
            )
            or 0
        )
        db_bytes = int(await conn.fetchval("SELECT pg_database_size(current_database())") or 0)

    memory = await client.info("memory")
    await client.aclose()
    await pool.close()

    latencies = sorted(float(row["latency_ms"]) for row in rows)

    def percentile(p: float) -> float:
        index = min(len(latencies) - 1, max(0, round((len(latencies) - 1) * p)))
        return latencies[index]

    elapsed = time.perf_counter() - started
    duplicate_rate = (count - delivery_count) / count
    report = f"""# TicketDrop synthetic pipeline measurement

Generated: {datetime.now(UTC).isoformat()}

This benchmark starts at Redis `XADD`; it excludes Resident Advisor network
time and the polling interval. Workload data is synthetic and clearly labeled.

- Host: {platform.platform()}
- Python: {platform.python_version()}
- Drops: {count}
- Load shape: {count} back-to-back `XADD` calls
- Harness concurrency: {os.environ.get("HARNESS_MAX_CONCURRENCY", "4 (default)")}
- Wall time: {elapsed:.3f} s
- Throughput: {count / elapsed:.2f} drops/s
- Latency p50: {percentile(0.50):.1f} ms
- Latency p95: {percentile(0.95):.1f} ms
- Latency p99: {percentile(0.99):.1f} ms
- Mean latency: {statistics.fmean(latencies):.1f} ms
- Duplicate/missing delivery rows: {duplicate_rate:.2%}
- Redis used memory after run: {int(memory.get("used_memory", 0)) / 1024 / 1024:.2f} MiB
- Postgres database size after run: {db_bytes / 1024 / 1024:.2f} MiB

Do not compare these numbers to source detection latency. At a five-second poll
interval, expected source detection delay alone is roughly 2.5 seconds.
"""
    await asyncio.to_thread(_write_report, output, report)
    print(report)


def _write_report(output: Path, report: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("docs/performance.md"))
    args = parser.parse_args()
    if args.count < 1 or args.count > 10_000:
        raise SystemExit("--count must be between 1 and 10000")
    asyncio.run(run(args.count, args.output))


if __name__ == "__main__":
    main()
