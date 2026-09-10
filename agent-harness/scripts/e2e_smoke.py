"""End-to-end smoke test against the live Docker Postgres and Redis.

Not part of the pytest suite on purpose: it opens real sockets, and the suite's
rule is that nothing in it does. Run it by hand after a change to the intake or
persistence path.

It publishes two synthetic messages shaped exactly like
ingest-service/internal/stream/publisher.go emits — one availability-tier
change and one discovery-tier announcement with the empty fingerprint that
marks it — runs the real harness over them, asserts the rows that appear, and
then removes everything it created.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta

import asyncpg
from redis.asyncio import Redis

from harness import app, config, log
from harness.config import DropState
from harness.embeddings import DeterministicEmbedder, to_pgvector

_log = log.get("e2e")

RA_EVENT = "e2e-ra-0001"
EDM_EVENT = "e2e-edm-0001"
ARTIST = "DVS1"
VENUE = "Spot Lite"
SOUND = [0.82, 0.31, -0.94, 0.11, 0.66, 0.05, -0.22, 0.48, 0.73, -0.61, 0.19, 0.37]
TAG = "e2e-smoke"


def uuid7() -> str:
    """Match the ids the Go publisher mints.

    Python 3.13 has no uuid.uuid7 and pulling a dependency for one line in a
    smoke script is not worth it: 48-bit millisecond timestamp, version 7,
    variant 0b10, random everywhere else.
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    hexed = f"{value:032x}"
    return f"{hexed[0:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:32]}"


async def seed(conn: asyncpg.Connection) -> dict[str, str]:
    """Insert exactly what the Go ingester would have written before publishing."""
    embedder = DeterministicEmbedder(dimension=1024)
    artist_vec, venue_vec = await embedder.embed([ARTIST, VENUE])

    venue_id = await conn.fetchval(
        """
        INSERT INTO venues (source, source_venue_id, name, area_name)
        VALUES ('ra', $1, $2, 'Detroit')
        ON CONFLICT (source, source_venue_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING id::text
        """,
        f"{TAG}-venue",
        VENUE,
    )

    artist_ids = []
    for source in ("ra", "edmtrain"):
        artist_ids.append(
            await conn.fetchval(
                """
                INSERT INTO artists (source, source_artist_id, name, semantic_embedding,
                                     sound_signature)
                VALUES ($1::source_platform, $2, $3, $4::vector, $5::vector)
                ON CONFLICT (source, source_artist_id) DO UPDATE
                SET semantic_embedding = EXCLUDED.semantic_embedding
                RETURNING id::text
                """,
                source,
                f"{TAG}-artist",
                ARTIST,
                to_pgvector(artist_vec),
                to_pgvector(SOUND),
            )
        )

    first_seen = datetime.now(UTC) - timedelta(hours=2)
    event_ids = {}
    for source, source_event_id, artist_id in (
        ("ra", RA_EVENT, artist_ids[0]),
        ("edmtrain", EDM_EVENT, artist_ids[1]),
    ):
        event_id = await conn.fetchval(
            """
            INSERT INTO events (source, source_event_id, title, content_url, ticket_link,
                                venue_id, starts_at, is_ticketed, attending, first_seen_at, raw)
            VALUES ($1::source_platform, $2, $3, $4, $5, $6::uuid, $7, true, 1000, $8, $9::jsonb)
            ON CONFLICT (source, source_event_id) DO UPDATE
            SET first_seen_at = EXCLUDED.first_seen_at
            RETURNING id::text
            """,
            source,
            source_event_id,
            "E2E Warehouse Session",
            f"https://{source}.example/events/{source_event_id}",
            f"https://vendor.example/checkout/{source_event_id}",
            venue_id,
            datetime.now(UTC) + timedelta(days=30),
            first_seen,
            json.dumps({"tag": TAG}),
        )
        await conn.execute(
            """
            INSERT INTO event_artists (event_id, artist_id, billing)
            VALUES ($1::uuid, $2::uuid, 0)
            ON CONFLICT DO NOTHING
            """,
            event_id,
            artist_id,
        )
        event_ids[source] = event_id

    # What the ingest resolver would have written. Without it the discovery
    # promotion has no availability-tier event to land on.
    await conn.execute(
        """
        INSERT INTO event_links (event_id, canonical_event_id, match_confidence, match_method)
        VALUES ($1::uuid, $2::uuid, 0.93, 'e2e:seeded')
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_ids["edmtrain"],
        event_ids["ra"],
    )

    for label, kind, embedding, signature in (
        (f"{TAG}-artist-pref", "artist", to_pgvector(artist_vec), None),
        (f"{TAG}-venue-pref", "venue", to_pgvector(venue_vec), None),
        (f"{TAG}-sound-pref", "sound", None, to_pgvector(SOUND)),
    ):
        await conn.execute(
            """
            INSERT INTO user_preferences (label, kind, embedding, sound_signature)
            VALUES ($1, $2::preference_kind, $3::vector, $4::vector)
            """,
            label,
            kind,
            embedding,
            signature,
        )

    return event_ids


def availability_message(drop_id: str) -> dict[str, str]:
    return {
        "drop_id": drop_id,
        "source": "ra",
        "event_id": RA_EVENT,
        "event_url": f"https://ra.example/events/{RA_EVENT}",
        "ticket_link": f"https://vendor.example/checkout/{RA_EVENT}",
        "title": "E2E Warehouse Session",
        "venue": VENUE,
        "starts_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "artists": ARTIST,
        "attending": "1000",
        "queue_it": "false",
        "change": "new_ticketed",
        "fingerprint": secrets.token_hex(32),
        "detected_at": datetime.now(UTC).isoformat(),
        "raw": json.dumps({"tag": TAG}),
    }


def announced_message(drop_id: str) -> dict[str, str]:
    """Discovery tier. Empty fingerprint and no attendance, as Go emits."""
    return {
        "drop_id": drop_id,
        "source": "edmtrain",
        "event_id": EDM_EVENT,
        "event_url": f"https://edmtrain.example/events/{EDM_EVENT}",
        "ticket_link": f"https://vendor.example/checkout/{EDM_EVENT}",
        "title": "E2E Warehouse Session",
        "venue": VENUE,
        "starts_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "artists": ARTIST,
        "attending": "",
        "queue_it": "false",
        "change": "announced",
        "fingerprint": "",
        "detected_at": datetime.now(UTC).isoformat(),
        "raw": json.dumps({"tag": TAG}),
    }


async def wait_for_terminal(conn: asyncpg.Connection, drop_ids: list[str], timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = await conn.fetch(
            "SELECT id::text, state::text FROM drops WHERE id = ANY($1::uuid[])", drop_ids
        )
        states = {row["id"]: row["state"] for row in rows}
        if len(states) == len(drop_ids) and all(
            state in {s.value for s in (DropState.NOTIFIED, DropState.SKIPPED, DropState.FAILED)}
            for state in states.values()
        ):
            return states
        await asyncio.sleep(0.25)
    return {}


async def report(
    conn: asyncpg.Connection, redis: Redis, result_stream: str, drop_ids: list[str]
) -> bool:
    ok = True
    drops = await conn.fetch(
        """
        SELECT d.id::text, d.change::text, d.state::text, d.hype_score, d.reservation_url,
               d.notified_at, e.source::text AS source
        FROM drops d JOIN events e ON e.id = d.event_id
        WHERE d.id = ANY($1::uuid[])
        ORDER BY d.created_at
        """,
        drop_ids,
    )
    print(f"\ndrops rows: {len(drops)}")
    for row in drops:
        print(
            f"  {row['source']:<8} {row['change']:<16} state={row['state']:<9} "
            f"hype={row['hype_score']} notified={row['notified_at'] is not None} "
            f"url={row['reservation_url']}"
        )

    transitions = await conn.fetch(
        """
        SELECT drop_id::text, from_state::text, to_state::text, latency_ms
        FROM drop_transitions WHERE drop_id = ANY($1::uuid[]) ORDER BY drop_id, at, id
        """,
        drop_ids,
    )
    print(f"\ndrop_transitions rows: {len(transitions)}")
    for row in transitions:
        print(
            f"  {row['drop_id'][:8]} {row['from_state']!s:<11} -> "
            f"{row['to_state']:<10} {row['latency_ms']} ms"
        )

    watchlist = await conn.fetch(
        """
        SELECT w.tier::text, w.watch_source::text, w.reason, e.source_event_id
        FROM watchlist w JOIN events e ON e.id = w.event_id
        WHERE e.source_event_id = ANY($1::text[])
        """,
        [RA_EVENT, EDM_EVENT],
    )
    print(f"\nwatchlist rows: {len(watchlist)}")
    for row in watchlist:
        print(
            f"  event={row['source_event_id']} tier={row['tier']} "
            f"source={row['watch_source']} reason={row['reason']}"
        )

    if watchlist and (
        watchlist[0]["watch_source"] != "ra" or watchlist[0]["source_event_id"] != RA_EVENT
    ):
        print("FAIL: promotion did not follow event_links onto the RA counterpart")
        ok = False

    results = [
        fields
        for _entry_id, fields in await redis.xrange(result_stream)
        if fields.get("drop_id") in drop_ids
    ]
    print(f"\n{result_stream} entries: {len(results)}")
    for fields in results:
        print(
            f"  state={fields.get('state')} outcome={fields.get('outcome')} "
            f"hype={fields.get('hype_score')} mode={fields.get('mode')}"
        )

    if len(results) != 2:
        print("FAIL: expected two entries on the results stream")
        ok = False
    if len(drops) != 2:
        print("FAIL: expected two drops rows")
        ok = False
    if len(transitions) < 6:
        print("FAIL: expected at least six transition rows")
        ok = False
    if len(watchlist) != 1:
        print("FAIL: expected exactly one watchlist promotion")
        ok = False
    if any(row["state"] != DropState.NOTIFIED.value for row in drops):
        print("FAIL: expected both drops to reach NOTIFIED")
        ok = False
    return ok


async def cleanup(
    conn: asyncpg.Connection,
    redis: Redis,
    entry_ids: list[str],
    drop_ids: list[str],
    stream: str,
    result_stream: str,
) -> None:
    await conn.execute(
        "DELETE FROM drops WHERE id = ANY($1::uuid[])", drop_ids
    )  # cascades drop_transitions
    await conn.execute(
        "DELETE FROM events WHERE source_event_id = ANY($1::text[])", [RA_EVENT, EDM_EVENT]
    )  # cascades watchlist and event_artists
    await conn.execute("DELETE FROM artists WHERE source_artist_id = $1", f"{TAG}-artist")
    await conn.execute("DELETE FROM venues WHERE source_venue_id = $1", f"{TAG}-venue")
    await conn.execute("DELETE FROM user_preferences WHERE label LIKE $1", f"{TAG}-%")

    if entry_ids:
        await redis.xdel(stream, *entry_ids)
    for drop_id in drop_ids:
        await redis.delete(f"drop:claim:{drop_id}")

    results = await redis.xrange(result_stream)
    stale = [entry_id for entry_id, fields in results if fields.get("drop_id") in drop_ids]
    if stale:
        await redis.xdel(result_stream, *stale)
    print(f"\ncleaned up: {len(entry_ids)} stream entries, {len(stale)} result entries")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the test rows in place")
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    os.environ.setdefault("HARNESS_CONSUMER_NAME", "e2e-smoke")
    cfg = config.load()
    log.configure(level=cfg.log_level, fmt="console")

    conn = await asyncpg.connect(cfg.database_url)
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    runtime = await app.build(cfg)

    entry_ids: list[str] = []
    drop_ids: list[str] = []
    ok = False
    stop = asyncio.Event()
    runner: asyncio.Task[None] | None = None
    try:
        await seed(conn)
        # The group is created at the stream tail, so it has to exist before
        # anything is published or the harness will never see these entries.
        await runtime.bus.ensure_group()

        for factory in (availability_message, announced_message):
            drop_id = uuid7()
            drop_ids.append(drop_id)
            entry_ids.append(await redis.xadd(cfg.stream, factory(drop_id)))
        print(f"published {len(entry_ids)} entries to {cfg.stream}")

        runner = asyncio.create_task(runtime.worker.run(stop))
        states = await wait_for_terminal(conn, drop_ids, args.timeout)
        if not states:
            print("FAIL: drops did not reach a terminal state in time")
        ok = await report(conn, redis, cfg.result_stream, drop_ids) and bool(states)
    finally:
        stop.set()
        if runner is not None:
            await asyncio.wait_for(runner, timeout=15)
        if not args.keep:
            await cleanup(conn, redis, entry_ids, drop_ids, cfg.stream, cfg.result_stream)
        await runtime.aclose()
        await redis.aclose()
        await conn.close()

    print("\nE2E:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
