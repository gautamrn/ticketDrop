"""Verify scout discovery, cold-start suppression, and idempotent delivery."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import asyncpg
import httpx
import redis.asyncio as redis

USER_ID = "33333333-3333-4333-8333-333333333333"
CHANNEL_ID = "44444444-4444-4444-8444-444444444444"


async def wait_for_value(
    pool: asyncpg.Pool[Any],
    sql: str,
    *args: object,
    expected: object | None = None,
    timeout_s: float = 45,
) -> object:
    deadline = time.monotonic() + timeout_s
    value: object = None
    while time.monotonic() < deadline:
        async with pool.acquire() as conn:
            value = await conn.fetchval(sql, *args)
        if expected is None:
            if value:
                return value
        elif value == expected:
            return value
        await asyncio.sleep(0.5)
    raise TimeoutError(f"timed out waiting for query; last value={value!r}")


async def wait_for_snapshot(
    client: redis.Redis,
    *,
    different_from: bytes | None = None,
    timeout_s: float = 15,
) -> bytes:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = await client.get("snap:ra:90001")
        if value is not None and value != different_from:
            return value
        await asyncio.sleep(0.25)
    raise TimeoutError("timed out waiting for RA snapshot cache update")


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    mock_url = os.environ.get("MOCK_RA_URL", "http://mock-ra:8098")
    pool = await asyncpg.create_pool(database_url)
    cache = redis.from_url(redis_url)
    try:
        event_id = str(
            await wait_for_value(
                pool,
                """
                SELECT id::text FROM events
                 WHERE source = 'ra' AND source_event_id = '90001'
                """,
            )
        )
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (id, email, name)
                VALUES ($1::uuid, 'smoke@example.com', 'Smoke user')
                """,
                USER_ID,
            )
            await conn.execute(
                """
                INSERT INTO user_settings (user_id, ra_area_id, ra_area_name)
                VALUES ($1::uuid, 19, 'Detroit')
                """,
                USER_ID,
            )
            await conn.execute(
                """
                INSERT INTO user_preferences (user_id, label, kind)
                VALUES ($1::uuid, 'Prospa', 'artist')
                """,
                USER_ID,
            )
            await conn.execute(
                """
                INSERT INTO notification_channels (
                    id, user_id, kind, destination, verified_at
                ) VALUES (
                    $1::uuid, $2::uuid, 'email',
                    'smoke@example.com', now()
                )
                """,
                CHANNEL_ID,
                USER_ID,
            )

        await wait_for_value(
            pool,
            """
            SELECT count(*)
              FROM event_subscriptions s
              JOIN events e ON e.id = s.event_id
             WHERE s.user_id=$1::uuid
               AND e.source_event_id='90001'
               AND s.active
               AND NOT s.user_disabled
               AND s.origin='scout'
            """,
            USER_ID,
            expected=1,
        )
        await wait_for_value(
            pool,
            "SELECT count(*) FROM watchlist WHERE event_id=$1::uuid AND active",
            event_id,
            expected=1,
        )
        await wait_for_snapshot(cache)
        async with pool.acquire() as conn:
            cold_start_drops = int(await conn.fetchval("SELECT count(*) FROM drops") or 0)
        if cold_start_drops != 0:
            raise AssertionError(f"cold start emitted {cold_start_drops} false drops")

        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(f"{mock_url}/admin/presale?id=90001")
            response.raise_for_status()

        await wait_for_value(
            pool,
            """
            SELECT count(*) FROM notification_deliveries
             WHERE user_id=$1::uuid AND status='sent'::delivery_status
            """,
            USER_ID,
            expected=1,
        )
        async with pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM drops
                    WHERE state='NOTIFIED') AS notified_drops,
                  (SELECT count(*) FROM drop_transitions t
                    JOIN drops d ON d.id=t.drop_id
                    WHERE d.state='NOTIFIED'
                      AND t.to_state='NOTIFIED'
                      AND t.detail->>'change'='tier_available') AS transitions,
                  (SELECT count(*) FROM notification_deliveries
                    WHERE user_id=$1::uuid AND status='sent') AS sent_deliveries
                """,
                USER_ID,
            )
        assert counts is not None
        if dict(counts) != {
            "notified_drops": 1,
            "transitions": 1,
            "sent_deliveries": 1,
        }:
            raise AssertionError(f"unexpected durable counts: {dict(counts)}")

        prior_snapshot = await cache.get("snap:ra:90001")
        assert prior_snapshot is not None
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(f"{mock_url}/admin/presale?id=90001")
            response.raise_for_status()
        await wait_for_snapshot(cache, different_from=prior_snapshot)
        async with pool.acquire() as conn:
            repeated = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM drops) AS drops,
                  (SELECT count(*) FROM drop_transitions) AS transitions,
                  (SELECT count(*) FROM notification_deliveries
                    WHERE user_id=$1::uuid) AS deliveries
                """,
                USER_ID,
            )
        assert repeated is not None
        if dict(repeated) != {"drops": 1, "transitions": 1, "deliveries": 1}:
            raise AssertionError(f"repeat mutation created duplicates: {dict(repeated)}")
        print(
            "product smoke passed: scout subscribed Prospa, baseline stayed silent, "
            "and one classified change produced one idempotent delivery"
        )
    finally:
        await cache.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
