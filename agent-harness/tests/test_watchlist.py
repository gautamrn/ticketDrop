"""Inert watchlist rows become pollable once event_links catches up.

Promotion-time follow only works if the link already exists. The resolver
runs off the hot path, so the common case is: write inert, link later,
repoint. These tests are the spec of that compensating write; PgStore
implements it in one statement, FakeStore in memory.
"""

from __future__ import annotations

from conftest import FakeStore, WatchTarget

EDM = "11111111-1111-4111-8111-111111111111"
RA = "22222222-2222-4222-8222-222222222222"


def _inert(**overrides: object) -> dict:
    row: dict = {
        "event_id": EDM,
        "watch_source": "edmtrain",
        "watch_url": "https://edmtrain.com/e/1",
        "reason": "hype 0.900 on announced; awaiting event_links resolution",
        "active": True,
        "event_source": "edmtrain",
    }
    row.update(overrides)
    return row


async def test_repoint_moves_an_inert_row_onto_the_linked_ra_event():
    store = FakeStore()
    store.watchlist = [_inert()]
    store.watch_target = WatchTarget(event_id=RA, source="ra", url="https://ra.co/events/4321")

    moved = await store.repoint_inert_watchlist()

    assert moved == 1
    assert len(store.watchlist) == 1
    assert store.watchlist[0]["event_id"] == RA
    assert store.watchlist[0]["watch_source"] == "ra"
    assert store.watchlist[0]["watch_url"] == "https://ra.co/events/4321"


async def test_repoint_leaves_an_unlinked_row_inert():
    """A missing counterpart is not evidence none exists — enumerate may not
    have stored it yet. Discarding the promotion would lose the drop."""
    store = FakeStore()
    store.watchlist = [_inert()]
    store.watch_target = None

    moved = await store.repoint_inert_watchlist()

    assert moved == 0
    assert store.watchlist[0]["event_id"] == EDM
    assert store.watchlist[0]["watch_source"] == "edmtrain"


async def test_repoint_is_a_no_op_for_already_pollable_rows():
    store = FakeStore()
    store.watchlist = [
        {
            "event_id": RA,
            "watch_source": "ra",
            "watch_url": "https://ra.co/events/4321",
            "reason": "hot",
            "active": True,
            "event_source": "ra",
        }
    ]
    store.watch_target = WatchTarget(event_id=RA, source="ra", url="https://ra.co/events/4321")

    moved = await store.repoint_inert_watchlist()

    assert moved == 0
    assert store.watchlist[0]["event_id"] == RA


async def test_repoint_flips_watch_source_when_the_event_is_already_ra():
    store = FakeStore()
    store.watchlist = [
        {
            "event_id": RA,
            "watch_source": "edmtrain",
            "watch_url": "https://ra.co/events/4321",
            "reason": "mis-tagged",
            "active": True,
            "event_source": "ra",
        }
    ]

    moved = await store.repoint_inert_watchlist()

    assert moved == 1
    assert len(store.watchlist) == 1
    assert store.watchlist[0]["event_id"] == RA
    assert store.watchlist[0]["watch_source"] == "ra"


async def test_repoint_skips_inactive_rows():
    store = FakeStore()
    store.watchlist = [_inert(active=False)]
    store.watch_target = WatchTarget(event_id=RA, source="ra", url="https://ra.co/events/1")

    moved = await store.repoint_inert_watchlist()

    assert moved == 0
    assert store.watchlist[0]["event_id"] == EDM
