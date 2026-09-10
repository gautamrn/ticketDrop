"""Fakes for everything that would otherwise be a network call.

The rule is absolute: no test in this suite opens a socket. The Store and Bus
protocols exist precisely so the FSM can be exercised against in-memory doubles
that record what was written, which is also what lets a test assert ordering
(that a transition row was written *before* the next node ran) rather than just
final state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from harness.browser.page import PageLike
from harness.config import Config, Deadlines, DropState, ReservationMode, ScoringWeights
from harness.messages import DropMessage, parse
from harness.store import AVAILABILITY_SOURCES, DemandSnapshot, DropInsert, EventRef, WatchTarget

EVENT_ID = "11111111-1111-4111-8111-111111111111"


# ------------------------------------------------------------------ messages


def stream_fields(**overrides: str) -> dict[str, str]:
    """Exactly the field set ingest-service/internal/stream/publisher.go emits."""
    fields = {
        "drop_id": "0192f0c0-0000-7000-8000-000000000001",
        "source": "ra",
        "event_id": "ra-1234",
        "event_url": "https://ra.co/events/1234",
        "ticket_link": "https://vendor.example/checkout/1234",
        "title": "Warehouse Session 04",
        "venue": "Spot Lite",
        "starts_at": "2026-09-01T22:00:00Z",
        "artists": "DVS1, Rrose, Anastasia Kristensen",
        "attending": "412",
        "queue_it": "false",
        "change": "new_ticketed",
        "fingerprint": "a" * 64,
        "detected_at": datetime.now(UTC).isoformat(),
        "raw": '{"id":"1234"}',
    }
    fields.update(overrides)
    return fields


def announced_fields(**overrides: str) -> dict[str, str]:
    """A discovery-tier message. Empty fingerprint is the marker, not an omission."""
    base = stream_fields(
        source="edmtrain",
        event_id="edm-9876",
        event_url="https://edmtrain.com/detroit/warehouse-session-04",
        change="announced",
        fingerprint="",
        attending="",
    )
    base.update(overrides)
    return base


def message(**overrides: str) -> DropMessage:
    return parse(stream_fields(**overrides))


# --------------------------------------------------------------------- store


@dataclass
class TransitionRow:
    drop_id: str
    from_state: DropState | None
    to_state: DropState
    latency_ms: int
    detail: dict[str, Any]


class FakeStore:
    def __init__(self) -> None:
        self.transitions: list[TransitionRow] = []
        self.updates: list[dict[str, Any]] = []
        self.drops: dict[str, DropState] = {}
        self.watchlist: list[dict[str, Any]] = []
        self.repoint_calls: int = 0
        self.repoint_error: Exception | None = None
        self.selectors: dict[tuple[str, str], str] = {}
        self.saved_selectors: list[dict[str, Any]] = []
        # Scripted responses. None means "signal unavailable", which is the
        # case the weighted blend has to degrade through rather than error on.
        self.preference_similarity: float | None = None
        self.sound_similarity: float | None = None
        self.sound_signature: list[float] | None = None
        self.demand = DemandSnapshot(attending=None, first_seen_at=None)
        self.insert_result = DropInsert.CREATED
        self.event_ref = EventRef(event_id=EVENT_ID, stubbed=False)
        self.watch_target: WatchTarget | None = None

    async def ensure_event(self, msg: DropMessage) -> EventRef:
        return self.event_ref

    async def create_drop(self, msg: DropMessage, event_id: str) -> DropInsert:
        if self.insert_result is DropInsert.CREATED:
            self.drops[msg.drop_id] = DropState.INGESTED
        return self.insert_result

    async def drop_state(self, drop_id: str) -> DropState | None:
        return self.drops.get(drop_id)

    async def record_transition(
        self,
        drop_id: str,
        from_state: DropState | None,
        to_state: DropState,
        latency_ms: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.transitions.append(
            TransitionRow(drop_id, from_state, to_state, latency_ms, detail or {})
        )

    async def update_drop(self, drop_id: str, **kwargs: Any) -> None:
        self.updates.append({"drop_id": drop_id, **kwargs})
        state = kwargs.get("state")
        if state is not None:
            self.drops[drop_id] = state

    async def best_preference_similarity(
        self, vector: Sequence[float], kinds: Sequence[str]
    ) -> float | None:
        return self.preference_similarity

    async def best_sound_similarity(self, signature: Sequence[float]) -> float | None:
        return self.sound_similarity

    async def event_sound_signature(self, event_id: str) -> list[float] | None:
        return self.sound_signature

    async def demand_snapshot(self, event_id: str) -> DemandSnapshot:
        return self.demand

    async def availability_watch_target(self, event_id: str) -> WatchTarget | None:
        return self.watch_target

    async def promote_to_watchlist(
        self, event_id: str, *, watch_source: str, watch_url: str, reason: str
    ) -> None:
        for row in self.watchlist:
            if row["event_id"] == event_id:
                row.update(
                    {
                        "watch_source": watch_source,
                        "watch_url": watch_url or row.get("watch_url"),
                        "reason": reason,
                        "active": True,
                    }
                )
                return
        self.watchlist.append(
            {
                "event_id": event_id,
                "watch_source": watch_source,
                "watch_url": watch_url,
                "reason": reason,
                "active": True,
                "event_source": watch_source,
            }
        )

    async def repoint_inert_watchlist(self) -> int:
        """In-memory equivalent of PgStore.repoint_inert_watchlist."""
        self.repoint_calls += 1
        if self.repoint_error is not None:
            raise self.repoint_error
        moved = 0
        remaining: list[dict[str, Any]] = []
        additions: list[dict[str, Any]] = []
        for row in self.watchlist:
            if not row.get("active", True):
                remaining.append(row)
                continue
            event_source = row.get("event_source", row["watch_source"])
            pollable = row["watch_source"] == event_source and event_source in AVAILABILITY_SOURCES
            if pollable:
                remaining.append(row)
                continue
            if event_source in AVAILABILITY_SOURCES:
                row["watch_source"] = event_source
                remaining.append(row)
                moved += 1
                continue
            target = await self.availability_watch_target(row["event_id"])
            if target is None or target.event_id == row["event_id"]:
                remaining.append(row)
                continue
            additions.append(
                {
                    "event_id": target.event_id,
                    "watch_source": target.source,
                    "watch_url": target.url,
                    "reason": f"{row.get('reason', '')} · repointed onto availability source",
                    "active": True,
                    "event_source": target.source,
                }
            )
            moved += 1
        self.watchlist = remaining
        for add in additions:
            await self.promote_to_watchlist(
                add["event_id"],
                watch_source=add["watch_source"],
                watch_url=add["watch_url"],
                reason=add["reason"],
            )
        return moved

    async def selector_override(self, domain: str, logical_name: str) -> str | None:
        return self.selectors.get((domain, logical_name))

    async def save_selector_override(self, **kwargs: Any) -> None:
        self.saved_selectors.append(kwargs)
        self.selectors[(kwargs["domain"], kwargs["logical_name"])] = kwargs["selector"]

    # Convenience for assertions.

    def states(self) -> list[DropState]:
        return [row.to_state for row in self.transitions]

    def final_state(self) -> DropState:
        return self.transitions[-1].to_state


# ----------------------------------------------------------------------- bus


class FakeBus:
    def __init__(self) -> None:
        self.acked: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.claims: dict[str, str] = {}
        self.cache: dict[tuple[str, str], str] = {}
        self.claim_succeeds = True
        self.group_created = False
        self.to_read: list[Any] = []
        self.to_reclaim: list[Any] = []

    async def ensure_group(self) -> None:
        self.group_created = True

    async def read(self) -> list[Any]:
        # Yields to the loop the way a blocking XREADGROUP would. Without it a
        # fake that returns instantly turns the worker's read loop into a spin
        # that never lets the stop signal be observed.
        await asyncio.sleep(0.005)
        return self.to_read.pop(0) if self.to_read else []

    async def reclaim(self) -> list[Any]:
        return self.to_reclaim.pop(0) if self.to_reclaim else []

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)

    async def claim_drop(self, drop_id: str) -> bool:
        if not self.claim_succeeds or drop_id in self.claims:
            return False
        self.claims[drop_id] = "test"
        return True

    async def publish_result(self, result: dict[str, Any]) -> None:
        self.results.append(result)

    async def cached_selector(self, domain: str, logical_name: str) -> str | None:
        return self.cache.get((domain, logical_name))

    async def cache_selector(self, domain: str, logical_name: str, selector: str) -> None:
        self.cache[(domain, logical_name)] = selector


# ---------------------------------------------------------------------- page


@dataclass
class FakeLocator:
    matches: int = 0
    visible: bool = True
    enabled: bool = True
    text: str = ""

    async def count(self) -> int:
        return self.matches

    async def is_visible(self, *, timeout: float | None = None) -> bool:
        return self.visible

    async def is_enabled(self, *, timeout: float | None = None) -> bool:
        return self.enabled

    async def text_content(self, *, timeout: float | None = None) -> str | None:
        return self.text

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        return None


@dataclass
class FakePage:
    """A page where only the selectors in `known` resolve to a single element."""

    url: str = "https://vendor.example/checkout/1234"
    html: str = "<html><body><button id='go'>Buy</button></body></html>"
    known: dict[str, FakeLocator] = field(default_factory=dict)
    content_error: Exception | None = None

    def locator(self, selector: str) -> FakeLocator:
        return self.known.get(selector, FakeLocator(matches=0))

    async def content(self) -> str:
        if self.content_error:
            raise self.content_error
        return self.html


def page_with(*selectors: str, url: str = "https://vendor.example/checkout/1234") -> PageLike:
    return FakePage(url=url, known={s: FakeLocator(matches=1) for s in selectors})


# -------------------------------------------------------------------- config


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "database_url": "postgres://unused",
        "redis_url": "redis://unused",
        "reservation_mode": ReservationMode.ALERT,
        "hype_threshold": 0.80,
        "watchlist_promote_threshold": 0.65,
        "self_heal_max_attempts": 1,
        "weights": ScoringWeights(),
        "deadlines": Deadlines(),
        "max_concurrency": 2,
    }
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def recent_first_seen() -> datetime:
    return datetime.now(UTC) - timedelta(hours=2)
