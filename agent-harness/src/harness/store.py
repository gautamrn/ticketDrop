"""Postgres persistence for scouting, delivery, and terminal drop audits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

import asyncpg

from .config import DropState
from .log import get
from .messages import DropMessage

_log = get("harness.store")


class DropInsert(StrEnum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class EventRef:
    event_id: str
    stubbed: bool


@dataclass(frozen=True, slots=True)
class Subscriber:
    user_id: str
    channel_id: str
    email: str


@dataclass(frozen=True, slots=True)
class Delivery:
    delivery_id: str
    status: str
    attempts: int


@dataclass(frozen=True, slots=True)
class AlertContext:
    title: str
    metro: str
    venue: str
    starts_at: datetime | None
    content_url: str
    ticket_link: str
    queue_it: bool


class Store(Protocol):
    async def ensure_event(self, msg: DropMessage) -> EventRef: ...
    async def create_drop(self, msg: DropMessage, event_id: str) -> DropInsert: ...
    async def drop_state(self, drop_id: str) -> DropState | None: ...
    async def finish_drop(
        self,
        drop_id: str,
        state: DropState,
        latency_ms: int,
        detail: dict[str, Any],
        error: str | None = None,
    ) -> None: ...
    async def dead_letter(self, drop_id: str, reason: str) -> None: ...
    async def subscribers(self, event_id: str) -> list[Subscriber]: ...
    async def alert_context(self, event_id: str) -> AlertContext: ...
    async def prepare_email_delivery(
        self, drop_id: str, subscriber: Subscriber
    ) -> Delivery | None: ...
    async def mark_delivery_sending(self, delivery_id: str) -> int: ...
    async def mark_delivery_sent(self, delivery_id: str, provider_message_id: str) -> None: ...
    async def mark_delivery_failed(
        self, delivery_id: str, error: str, *, retry_in_seconds: float | None
    ) -> None: ...


class PgStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 8) -> PgStore:
        pool = await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)
        if pool is None:
            raise RuntimeError("store: could not create connection pool")
        return cls(pool)

    async def aclose(self) -> None:
        await self._pool.close()

    async def ping(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT 1")

    async def ensure_event(self, msg: DropMessage) -> EventRef:
        async with self._pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id::text FROM events WHERE source = $1 AND source_event_id = $2",
                msg.source,
                msg.source_event_id,
            )
            if existing:
                return EventRef(existing, False)
            event_id = await conn.fetchval(
                """
                INSERT INTO events (
                    source, source_event_id, title, content_url, ticket_link,
                    starts_at, queue_it_enabled, attending, raw
                ) VALUES ('ra', $1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                ON CONFLICT (source, source_event_id) DO UPDATE
                   SET last_seen_at = now()
                RETURNING id::text
                """,
                msg.source_event_id,
                msg.title or f"ra:{msg.source_event_id}",
                msg.event_url or None,
                msg.ticket_link or None,
                msg.starts_at,
                msg.queue_it,
                msg.attending,
                msg.raw or "{}",
            )
        _log.warning(
            "event absent from catalogue, inserted stub",
            source_event_id=msg.source_event_id,
            event_id=event_id,
        )
        return EventRef(event_id, True)

    async def create_drop(self, msg: DropMessage, event_id: str) -> DropInsert:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO drops (id, event_id, change, fingerprint, detected_at)
                    VALUES ($1::uuid, $2::uuid, $3::change_kind, $4, $5)
                    """,
                    msg.drop_id,
                    event_id,
                    msg.change,
                    msg.fingerprint,
                    msg.detected_at,
                )
            return DropInsert.CREATED
        except asyncpg.UniqueViolationError as exc:
            return (
                DropInsert.ALREADY_EXISTS
                if exc.constraint_name == "drops_pkey"
                else DropInsert.DUPLICATE
            )

    async def drop_state(self, drop_id: str) -> DropState | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT state::text FROM drops WHERE id = $1::uuid", drop_id
            )
        return DropState(value) if value else None

    async def finish_drop(
        self,
        drop_id: str,
        state: DropState,
        latency_ms: int,
        detail: dict[str, Any],
        error: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE drops
                   SET state = $2::drop_state,
                       error = $3,
                       notified_at = CASE WHEN $2 = 'NOTIFIED' THEN now() ELSE notified_at END
                 WHERE id = $1::uuid
                """,
                drop_id,
                state.value,
                error[:2000] if error else None,
            )
            await conn.execute(
                """
                INSERT INTO drop_transitions (drop_id, to_state, latency_ms, detail)
                VALUES ($1::uuid, $2::drop_state, $3, $4::jsonb)
                """,
                drop_id,
                state.value,
                latency_ms,
                json.dumps(detail),
            )

    async def dead_letter(self, drop_id: str, reason: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE drops SET state = 'FAILED', error = $2
                 WHERE id = $1::uuid AND state IS NULL
                RETURNING id
                """,
                drop_id,
                reason[:2000],
            )
            if updated:
                await conn.execute(
                    """
                    INSERT INTO drop_transitions (drop_id, to_state, latency_ms, detail)
                    VALUES ($1::uuid, 'FAILED', 0, $2::jsonb)
                    """,
                    drop_id,
                    json.dumps({"dead_letter": True, "error": reason}),
                )

    async def subscribers(self, event_id: str) -> list[Subscriber]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.user_id::text, nc.id::text AS channel_id, nc.destination
                  FROM event_subscriptions s
                  JOIN users u ON u.id = s.user_id
                  JOIN LATERAL (
                      SELECT id, destination
                        FROM notification_channels
                       WHERE user_id = s.user_id
                         AND kind = 'email'
                         AND active
                         AND verified_at IS NOT NULL
                         AND lower(destination) = lower(u.email)
                       ORDER BY verified_at DESC, created_at
                       LIMIT 1
                  ) nc ON true
                 WHERE s.event_id = $1::uuid
                   AND s.active
                   AND NOT s.user_disabled
                 ORDER BY s.user_id
                """,
                event_id,
            )
        return [Subscriber(r["user_id"], r["channel_id"], r["destination"]) for r in rows]

    async def alert_context(self, event_id: str) -> AlertContext:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT e.title, COALESCE(v.area_name, '') AS metro,
                       COALESCE(v.name, '') AS venue, e.starts_at,
                       COALESCE(e.content_url, '') AS content_url,
                       COALESCE(e.ticket_link, '') AS ticket_link,
                       e.queue_it_enabled
                  FROM events e
                  LEFT JOIN venues v ON v.id = e.venue_id
                 WHERE e.id = $1::uuid
                """,
                event_id,
            )
        if row is None:
            raise RuntimeError(f"event {event_id} disappeared")
        return AlertContext(
            row["title"],
            row["metro"],
            row["venue"],
            row["starts_at"],
            row["content_url"],
            row["ticket_link"],
            row["queue_it_enabled"],
        )

    async def prepare_email_delivery(self, drop_id: str, subscriber: Subscriber) -> Delivery | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO notification_deliveries (
                    drop_id, user_id, channel_id, channel, status
                ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'email', 'pending')
                ON CONFLICT (drop_id, user_id, channel) DO UPDATE
                   SET updated_at = notification_deliveries.updated_at
                RETURNING id::text, status::text, attempts
                """,
                drop_id,
                subscriber.user_id,
                subscriber.channel_id,
            )
        return Delivery(row["id"], row["status"], int(row["attempts"])) if row else None

    async def mark_delivery_sending(self, delivery_id: str) -> int:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                UPDATE notification_deliveries
                   SET status = 'sending', attempts = attempts + 1,
                       last_error = NULL, next_attempt_at = NULL
                 WHERE id = $1::uuid
                RETURNING attempts
                """,
                delivery_id,
            )
        return int(value or 0)

    async def mark_delivery_sent(self, delivery_id: str, provider_message_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE notification_deliveries
                   SET status = 'sent', provider_message_id = $2, sent_at = now(),
                       next_attempt_at = NULL, last_error = NULL
                 WHERE id = $1::uuid
                """,
                delivery_id,
                provider_message_id,
            )

    async def mark_delivery_failed(
        self, delivery_id: str, error: str, *, retry_in_seconds: float | None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE notification_deliveries
                   SET status = 'failed', last_error = $2,
                       next_attempt_at = CASE
                         WHEN $3::float IS NULL THEN NULL
                         ELSE now() + ($3 * interval '1 second')
                       END
                 WHERE id = $1::uuid
                """,
                delivery_id,
                error[:2000],
                retry_in_seconds,
            )

    async def reconcile_subscriptions(self) -> int:
        async with self._pool.acquire() as conn:
            changed = await conn.fetchval(
                """
                WITH subscribed AS (
                    SELECT event_id, count(*)::int AS subscribers
                      FROM event_subscriptions
                     WHERE active AND NOT user_disabled
                     GROUP BY event_id
                ), upserted AS (
                    INSERT INTO watchlist (event_id, tier, reason, active)
                    SELECT event_id, 'hot', 'user subscriptions: ' || subscribers, true
                      FROM subscribed
                    ON CONFLICT (event_id) DO UPDATE SET
                        tier = 'hot', reason = EXCLUDED.reason, active = true
                    RETURNING event_id
                ), deactivated AS (
                    UPDATE watchlist w SET active = false, reason = 'user subscriptions: 0'
                     WHERE w.reason LIKE 'user subscriptions:%'
                       AND w.active
                       AND NOT EXISTS (
                           SELECT 1 FROM subscribed s WHERE s.event_id = w.event_id
                       )
                    RETURNING event_id
                )
                SELECT (SELECT count(*) FROM upserted)
                     + (SELECT count(*) FROM deactivated)
                """
            )
        return int(changed or 0)

    async def scout_subscriptions(self) -> int:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            added = await conn.fetchval(
                """
                WITH candidates AS (
                    SELECT DISTINCT us.user_id, e.id AS event_id, e.starts_at
                      FROM user_settings us
                      JOIN events e ON e.source = 'ra'
                      JOIN venues v ON v.id = e.venue_id
                       AND v.area_id = us.ra_area_id
                     WHERE us.ra_area_id IS NOT NULL
                       AND e.starts_at >= now() - interval '1 day'
                       AND EXISTS (
                           SELECT 1
                             FROM user_preferences p
                            WHERE p.user_id = us.user_id AND p.active
                              AND (
                                (p.kind = 'venue'
                                 AND lower(regexp_replace(btrim(p.label), '\\s+', ' ', 'g'))
                                     = lower(regexp_replace(btrim(v.name), '\\s+', ' ', 'g')))
                                OR
                                (p.kind = 'artist' AND EXISTS (
                                    SELECT 1
                                      FROM event_artists ea
                                      JOIN artists a ON a.id = ea.artist_id
                                     WHERE ea.event_id = e.id
                                       AND lower(regexp_replace(btrim(p.label), '\\s+', ' ', 'g'))
                                           = lower(regexp_replace(btrim(a.name), '\\s+', ' ', 'g'))
                                ))
                              )
                       )
                ), ranked AS (
                    SELECT c.*,
                           row_number() OVER (
                               PARTITION BY c.user_id ORDER BY c.starts_at, c.event_id
                           ) AS candidate_rank,
                           (
                               SELECT count(*) FROM event_subscriptions es
                                WHERE es.user_id = c.user_id AND es.active
                           ) AS active_count
                      FROM candidates c
                     WHERE NOT EXISTS (
                         SELECT 1 FROM event_subscriptions es
                          WHERE es.user_id = c.user_id AND es.event_id = c.event_id
                     )
                ), inserted AS (
                    INSERT INTO event_subscriptions (
                        user_id, event_id, active, origin, user_disabled
                    )
                    SELECT user_id, event_id, true, 'scout', false
                      FROM ranked
                     WHERE active_count + candidate_rank <= 25
                    ON CONFLICT (user_id, event_id) DO NOTHING
                    RETURNING id
                )
                SELECT count(*) FROM inserted
                """
            )
        return int(added or 0)
