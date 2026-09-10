"""All Postgres access for the harness.

The harness owns `drops`, `drop_transitions`, and `watchlist` promotion, and
shares `selector_overrides` with nothing. It reads the catalogue tables that
the Go ingester writes. Every statement casts enum parameters explicitly
because asyncpg will not infer a Postgres enum from a Python str.

The Store protocol is the seam the tests bind to: nothing below it may be
exercised in a unit test, since it is all network.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

import asyncpg

from .config import DropState
from .embeddings import to_pgvector
from .log import get
from .messages import DropMessage

_log = get("harness.store")


class DropInsert(StrEnum):
    CREATED = "created"
    # Same drop_id already present: a redelivery that outlived its claim key.
    ALREADY_EXISTS = "already_exists"
    # A different drop already recorded this exact availability state. The
    # unique index on (event_id, fingerprint) is the dedupe guard from the
    # schema, and hitting it means the poll loop saw one change twice.
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class EventRef:
    event_id: str
    # True when the harness had to synthesize the row because the ingester's
    # copy was absent. Recorded in the transition detail so an audit can tell a
    # real catalogue row from a placeholder.
    stubbed: bool


@dataclass(frozen=True, slots=True)
class DemandSnapshot:
    attending: int | None
    first_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class WatchTarget:
    event_id: str
    source: str
    url: str


# Sources that can answer "did the sale state change". EDMTrain is a listings
# site with no inventory concept, so it can discover a show but never watch it.
AVAILABILITY_SOURCES = ("ra",)


class Store(Protocol):
    async def ensure_event(self, msg: DropMessage) -> EventRef: ...

    async def create_drop(self, msg: DropMessage, event_id: str) -> DropInsert: ...

    async def drop_state(self, drop_id: str) -> DropState | None: ...

    async def record_transition(
        self,
        drop_id: str,
        from_state: DropState | None,
        to_state: DropState,
        latency_ms: int,
        detail: dict[str, Any] | None = None,
    ) -> None: ...

    async def update_drop(
        self,
        drop_id: str,
        *,
        state: DropState | None = None,
        hype_score: float | None = None,
        attempt_count: int | None = None,
        reservation_url: str | None = None,
        error: str | None = None,
        notified: bool = False,
    ) -> None: ...

    async def best_preference_similarity(
        self, vector: Sequence[float], kinds: Sequence[str]
    ) -> float | None: ...

    async def best_sound_similarity(self, signature: Sequence[float]) -> float | None: ...

    async def event_sound_signature(self, event_id: str) -> list[float] | None: ...

    async def demand_snapshot(self, event_id: str) -> DemandSnapshot: ...

    async def availability_watch_target(self, event_id: str) -> WatchTarget | None: ...

    async def promote_to_watchlist(
        self, event_id: str, *, watch_source: str, watch_url: str, reason: str
    ) -> None: ...

    async def repoint_inert_watchlist(self) -> int: ...

    async def selector_override(self, domain: str, logical_name: str) -> str | None: ...

    async def save_selector_override(
        self,
        *,
        domain: str,
        logical_name: str,
        selector: str,
        confidence: float | None,
        model: str,
        healed_from: str,
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

    # ------------------------------------------------------------- catalogue

    async def ensure_event(self, msg: DropMessage) -> EventRef:
        """Resolve events.id from (source, source_event_id), inserting a stub if absent.

        The Go ingester upserts the event before it publishes, so the lookup
        normally hits. It can miss after a catalogue row is deleted or when a
        stream entry is replayed against a fresh database, and `drops.event_id`
        is NOT NULL — without a row there is nowhere to record that the drop
        even happened. A stub keeps the audit trail intact and is corrected by
        the ingester's next upsert, which is strictly better than dropping the
        message on the floor.
        """
        async with self._pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT id::text FROM events
                WHERE source = $1::source_platform AND source_event_id = $2
                """,
                msg.source,
                msg.source_event_id,
            )
            if existing:
                return EventRef(event_id=existing, stubbed=False)

            event_id = await conn.fetchval(
                """
                INSERT INTO events (
                    source, source_event_id, title, content_url, ticket_link,
                    starts_at, queue_it_enabled, attending, raw
                ) VALUES (
                    $1::source_platform, $2, $3, $4, $5, $6, $7, $8, $9::jsonb
                )
                ON CONFLICT (source, source_event_id) DO UPDATE
                SET last_seen_at = now()
                RETURNING id::text
                """,
                msg.source,
                msg.source_event_id,
                msg.title or f"{msg.source}:{msg.source_event_id}",
                msg.event_url or None,
                msg.ticket_link or None,
                msg.starts_at,
                msg.queue_it,
                msg.attending,
                msg.raw or "{}",
            )
            _log.warning(
                "event absent from catalogue, inserted stub",
                source=msg.source,
                source_event_id=msg.source_event_id,
                event_id=event_id,
            )
            return EventRef(event_id=event_id, stubbed=True)

    # -------------------------------------------------------------- fsm rows

    async def create_drop(self, msg: DropMessage, event_id: str) -> DropInsert:
        """Insert the FSM instance. Two unique constraints, two distinct outcomes.

        ON CONFLICT cannot target both the primary key and the
        (event_id, fingerprint) dedupe index in one statement, so the violation
        is caught and classified instead. The distinction matters: a repeated
        drop_id is our own redelivery, a repeated fingerprint is the poll loop
        reporting one change twice and must not produce a second alert.
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO drops (
                        id, event_id, change, fingerprint, state, detected_at
                    ) VALUES (
                        $1::uuid, $2::uuid, $3::change_kind, $4,
                        'INGESTED'::drop_state, $5
                    )
                    """,
                    msg.drop_id,
                    event_id,
                    msg.change,
                    msg.fingerprint,
                    msg.detected_at,
                )
            return DropInsert.CREATED
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "drops_pkey":
                return DropInsert.ALREADY_EXISTS
            return DropInsert.DUPLICATE

    async def drop_state(self, drop_id: str) -> DropState | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT state::text FROM drops WHERE id = $1::uuid", drop_id
            )
        return DropState(value) if value else None

    async def record_transition(
        self,
        drop_id: str,
        from_state: DropState | None,
        to_state: DropState,
        latency_ms: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO drop_transitions (drop_id, from_state, to_state, latency_ms, detail)
                VALUES ($1::uuid, $2::drop_state, $3::drop_state, $4, $5::jsonb)
                """,
                drop_id,
                from_state.value if from_state else None,
                to_state.value,
                latency_ms,
                json.dumps(detail or {}),
            )

    async def update_drop(
        self,
        drop_id: str,
        *,
        state: DropState | None = None,
        hype_score: float | None = None,
        attempt_count: int | None = None,
        reservation_url: str | None = None,
        error: str | None = None,
        notified: bool = False,
    ) -> None:
        sets: list[str] = []
        args: list[Any] = []

        def add(fragment: str, value: Any) -> None:
            args.append(value)
            sets.append(fragment.format(n=len(args) + 1))

        if state is not None:
            add("state = ${n}::drop_state", state.value)
            sets.append("state_entered_at = now()")
        if hype_score is not None:
            add("hype_score = ${n}::numeric", Decimal(f"{hype_score:.3f}"))
        if attempt_count is not None:
            add("attempt_count = ${n}", attempt_count)
        if reservation_url is not None:
            add("reservation_url = ${n}", reservation_url)
        if error is not None:
            # Truncated: an error column is for triage, not for a page of stack.
            add("error = ${n}", error[:2000])
        if notified:
            sets.append("notified_at = now()")
        if not sets:
            return

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE drops SET {', '.join(sets)} WHERE id = $1::uuid",
                drop_id,
                *args,
            )

    # -------------------------------------------------------------- scoring

    async def best_preference_similarity(
        self, vector: Sequence[float], kinds: Sequence[str]
    ) -> float | None:
        """Max cosine similarity against the active preference set.

        `<=>` is cosine distance, so similarity is 1 - distance. Ordering by
        the operator (not by the computed similarity) is what lets the HNSW
        index answer this without a sequential scan.
        """
        async with self._pool.acquire() as conn:
            distance = await conn.fetchval(
                """
                SELECT embedding <=> $1::vector AS distance
                FROM user_preferences
                WHERE active AND embedding IS NOT NULL
                  AND kind = ANY($2::preference_kind[])
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                to_pgvector(vector),
                list(kinds),
            )
        return None if distance is None else 1.0 - float(distance)

    async def count_active_preferences(self) -> int:
        """Used at startup to detect the silently-inert configuration: scoring
        fails closed, so an empty preference set means nothing ever reserves."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval("SELECT count(*) FROM user_preferences WHERE active") or 0
            )

    async def sound_scoring_available(self) -> bool:
        """Whether the sound term can ever be earned in this deployment.

        Both sides must exist: an artist fingerprint to compare and a sound
        preference to compare it against. Nothing currently populates
        `artists.sound_signature` — it needs audio analysis this system does not
        do — so this is normally false, and the scorer must not charge every
        event for a signal no event can produce.
        """
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (SELECT 1 FROM artists WHERE sound_signature IS NOT NULL)
                       AND EXISTS (
                           SELECT 1 FROM user_preferences
                           WHERE active AND kind = 'sound'::preference_kind
                             AND sound_signature IS NOT NULL
                       )
                    """
                )
            )

    async def add_preference(
        self, *, label: str, kind: str, vector: Sequence[float], weight: float = 1.0
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_preferences (label, kind, embedding, weight)
                VALUES ($1, $2::preference_kind, $3::vector, $4)
                """,
                label,
                kind,
                to_pgvector(vector),
                weight,
            )

    async def list_preferences(self) -> list[Any]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT label, kind::text AS kind, weight, active
                FROM user_preferences
                ORDER BY kind, label
                """
            )

    async def best_sound_similarity(self, signature: Sequence[float]) -> float | None:
        async with self._pool.acquire() as conn:
            distance = await conn.fetchval(
                """
                SELECT sound_signature <=> $1::vector AS distance
                FROM user_preferences
                WHERE active AND sound_signature IS NOT NULL
                  AND kind = 'sound'::preference_kind
                ORDER BY sound_signature <=> $1::vector
                LIMIT 1
                """,
                to_pgvector(signature),
            )
        return None if distance is None else 1.0 - float(distance)

    async def event_sound_signature(self, event_id: str) -> list[float] | None:
        """Mean audio fingerprint over the lineup.

        Averaging is crude for a b2b bill, but the alternative (max over
        artists) rewards a single on-genre support act on an otherwise wrong
        lineup, which is the failure this signal exists to catch.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT AVG(a.sound_signature)::text
                FROM event_artists ea
                JOIN artists a ON a.id = ea.artist_id
                WHERE ea.event_id = $1::uuid AND a.sound_signature IS NOT NULL
                """,
                event_id,
            )
        if not row:
            return None
        return [float(x) for x in row.strip("[]").split(",")]

    async def demand_snapshot(self, event_id: str) -> DemandSnapshot:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT attending, first_seen_at FROM events WHERE id = $1::uuid",
                event_id,
            )
        if row is None:
            return DemandSnapshot(attending=None, first_seen_at=None)
        return DemandSnapshot(attending=row["attending"], first_seen_at=row["first_seen_at"])

    # ------------------------------------------------------------ watchlist

    async def availability_watch_target(self, event_id: str) -> WatchTarget | None:
        """Find the availability-tier row that stands in for this event.

        The Go poller's due-items query requires `watch_source` to match the
        watchlisted event's own source, so watchlisting an EDMTrain event under
        'edmtrain' produces a row nothing will ever poll. The cross-source link
        the ingest resolver writes into `event_links` is what makes "EDMTrain
        finds it, RA watches it" actually work.

        Both link directions are checked because the resolver elects a
        canonical row and which side of the pair we started from depends on
        which source discovered the show first.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT e.id::text AS event_id,
                       e.source::text AS source,
                       COALESCE(e.content_url, e.ticket_link, '') AS url
                FROM event_links l
                JOIN events e ON e.id = CASE
                    WHEN l.event_id = $1::uuid THEN l.canonical_event_id
                    ELSE l.event_id
                END
                WHERE $1::uuid IN (l.event_id, l.canonical_event_id)
                  AND e.source = ANY($2::source_platform[])
                ORDER BY l.match_confidence DESC NULLS LAST
                LIMIT 1
                """,
                event_id,
                list(AVAILABILITY_SOURCES),
            )
        if row is None:
            return None
        return WatchTarget(event_id=row["event_id"], source=row["source"], url=row["url"])

    async def promote_to_watchlist(
        self, event_id: str, *, watch_source: str, watch_url: str, reason: str
    ) -> None:
        """Give an announced show a fast-poll slot.

        Re-promotion upgrades the tier and reactivates a row that was retired,
        because the reason a show earns `hot` twice is that it got hotter.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO watchlist (event_id, tier, watch_source, watch_url, reason, active)
                VALUES ($1::uuid, 'hot'::watch_tier, $2::source_platform, $3, $4, true)
                ON CONFLICT (event_id) DO UPDATE SET
                    tier   = 'hot'::watch_tier,
                    active = true,
                    reason = EXCLUDED.reason,
                    watch_url = COALESCE(EXCLUDED.watch_url, watchlist.watch_url)
                """,
                event_id,
                watch_source,
                watch_url or None,
                reason,
            )

    async def repoint_inert_watchlist(self) -> int:
        """Move incomplete promotions onto an availability-tier event once a link exists.

        Promotion writes an inert row when `event_links` has not caught up yet —
        the resolver runs off the hot path. The resolver cannot update
        `watchlist` (harness-owned), so this is the compensating write: the
        same service that created the incomplete row finishes it.

        Two shapes of inert:
        - the watchlisted event is already RA but `watch_source` is wrong
          → UPDATE in place
        - the watchlisted event is EDMTrain and a link now points at RA
          → INSERT the RA row, DELETE the EDMTrain row (`event_id` is the PK)

        Distinct from promotion-time follow: that path only fires if the link
        already exists. This path heals the lag.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH inert AS (
                    SELECT w.event_id AS from_id,
                           COALESCE(w.reason, '') AS reason,
                           COALESCE(w.watch_url, '') AS watch_url,
                           e.source::text AS event_source
                    FROM watchlist w
                    JOIN events e ON e.id = w.event_id
                    WHERE w.active
                      AND NOT (
                          w.watch_source = e.source
                          AND e.source = ANY($1::source_platform[])
                      )
                ),
                same_source AS (
                    SELECT from_id, from_id AS to_id, event_source AS to_source,
                           watch_url, reason
                    FROM inert
                    WHERE event_source::source_platform = ANY($1::source_platform[])
                ),
                linked AS (
                    SELECT DISTINCT ON (i.from_id)
                           i.from_id,
                           t.id AS to_id,
                           t.source::text AS to_source,
                           COALESCE(t.content_url, t.ticket_link, i.watch_url) AS watch_url,
                           i.reason
                    FROM inert i
                    JOIN event_links l
                      ON i.from_id IN (l.event_id, l.canonical_event_id)
                    JOIN events t ON t.id = CASE
                        WHEN l.event_id = i.from_id THEN l.canonical_event_id
                        ELSE l.event_id
                    END
                    WHERE i.event_source::source_platform <> ALL ($1::source_platform[])
                      AND t.source = ANY($1::source_platform[])
                      AND t.id <> i.from_id
                    ORDER BY i.from_id, l.match_confidence DESC NULLS LAST
                ),
                moves AS (
                    SELECT * FROM same_source
                    UNION ALL
                    SELECT * FROM linked
                ),
                upserted AS (
                    INSERT INTO watchlist (
                        event_id, tier, watch_source, watch_url, reason, active
                    )
                    SELECT to_id,
                           'hot'::watch_tier,
                           to_source::source_platform,
                           NULLIF(watch_url, ''),
                           CASE
                               WHEN from_id = to_id THEN reason
                               ELSE reason || ' · repointed onto availability source'
                           END,
                           true
                    FROM moves
                    ON CONFLICT (event_id) DO UPDATE SET
                        tier         = 'hot'::watch_tier,
                        active       = true,
                        watch_source = EXCLUDED.watch_source,
                        watch_url    = COALESCE(EXCLUDED.watch_url, watchlist.watch_url),
                        reason       = EXCLUDED.reason
                    RETURNING event_id
                ),
                dropped AS (
                    DELETE FROM watchlist w
                    USING moves m
                    WHERE w.event_id = m.from_id
                      AND m.from_id <> m.to_id
                    RETURNING w.event_id
                )
                SELECT (SELECT count(*) FROM upserted)::int AS moved
                """,
                list(AVAILABILITY_SOURCES),
            )
        return int(row["moved"]) if row is not None else 0

    # ---------------------------------------------------- selector overrides

    async def selector_override(self, domain: str, logical_name: str) -> str | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT selector FROM selector_overrides WHERE domain = $1 AND logical_name = $2",
                domain,
                logical_name,
            )

    async def save_selector_override(
        self,
        *,
        domain: str,
        logical_name: str,
        selector: str,
        confidence: float | None,
        model: str,
        healed_from: str,
    ) -> None:
        """Durable mirror of the Redis hash.

        verified_at is set here and only here: the row is written after in-page
        validation passed, so its presence means a human auditor can trust the
        selector actually matched something once.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO selector_overrides (
                    domain, logical_name, selector, confidence, model, healed_from, verified_at
                ) VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (domain, logical_name) DO UPDATE SET
                    selector    = EXCLUDED.selector,
                    confidence  = EXCLUDED.confidence,
                    model       = EXCLUDED.model,
                    healed_from = EXCLUDED.healed_from,
                    verified_at = now()
                """,
                domain,
                logical_name,
                selector,
                confidence,
                model,
                healed_from[:500],
            )
