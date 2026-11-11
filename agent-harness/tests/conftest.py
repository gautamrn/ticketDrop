from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from harness.config import Config, DropState
from harness.messages import DropMessage, parse
from harness.store import AlertContext, Delivery, DropInsert, EventRef, Subscriber

EVENT_ID = "11111111-1111-4111-8111-111111111111"


def stream_fields(**overrides: str) -> dict[str, str]:
    fields = {
        "drop_id": "0192f0c0-0000-7000-8000-000000000001",
        "source": "ra",
        "event_id": "90001",
        "event_url": "https://ra.co/events/90001",
        "ticket_link": "https://seller.example/90001",
        "title": "Warehouse Session",
        "venue": "Spot Lite",
        "starts_at": "2026-09-01T22:00:00Z",
        "artists": "Prospa",
        "attending": "412",
        "queue_it": "false",
        "change": "new_ticketed",
        "fingerprint": "a" * 64,
        "detected_at": datetime.now(UTC).isoformat(),
        "raw": '{"id":"90001"}',
    }
    fields.update(overrides)
    return fields


def message(**overrides: str) -> DropMessage:
    return parse(stream_fields(**overrides))


class FakeBus:
    def __init__(self) -> None:
        self.acked: list[str] = []
        self.claim_succeeds = True
        self.group_created = False

    async def ensure_group(self) -> None:
        self.group_created = True

    async def read(self) -> list[Any]:
        await asyncio.sleep(0)
        return []

    async def reclaim(self) -> list[Any]:
        return []

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)

    async def claim_drop(self, drop_id: str) -> bool:
        return self.claim_succeeds


class FakeStore:
    def __init__(self) -> None:
        self.insert_result = DropInsert.CREATED
        self.event_ref = EventRef(EVENT_ID, False)
        self.states: dict[str, DropState | None] = {}
        self.finished: list[tuple[str, DropState, int, dict[str, Any]]] = []
        self.dead_letters: list[tuple[str, str]] = []
        self.subscriber_rows: list[Subscriber] = []
        self.context = AlertContext(
            "Warehouse Session",
            "Detroit",
            "Spot Lite",
            datetime(2026, 9, 1, 22, tzinfo=UTC),
            "https://ra.co/events/90001",
            "https://seller.example/90001",
            False,
        )
        self.deliveries: dict[tuple[str, str], dict[str, Any]] = {}

    async def ensure_event(self, msg: DropMessage) -> EventRef:
        return self.event_ref

    async def create_drop(self, msg: DropMessage, event_id: str) -> DropInsert:
        self.states.setdefault(msg.drop_id, None)
        return self.insert_result

    async def drop_state(self, drop_id: str) -> DropState | None:
        return self.states.get(drop_id)

    async def finish_drop(
        self,
        drop_id: str,
        state: DropState,
        latency_ms: int,
        detail: dict[str, Any],
        error: str | None = None,
    ) -> None:
        self.states[drop_id] = state
        self.finished.append((drop_id, state, latency_ms, detail))

    async def dead_letter(self, drop_id: str, reason: str) -> None:
        self.dead_letters.append((drop_id, reason))

    async def subscribers(self, event_id: str) -> list[Subscriber]:
        return self.subscriber_rows

    async def alert_context(self, event_id: str) -> AlertContext:
        return self.context

    async def prepare_email_delivery(self, drop_id: str, subscriber: Subscriber) -> Delivery:
        row = self.deliveries.setdefault(
            (drop_id, subscriber.user_id),
            {"id": f"d{len(self.deliveries) + 1}", "status": "pending", "attempts": 0},
        )
        return Delivery(row["id"], row["status"], row["attempts"])

    async def mark_delivery_sending(self, delivery_id: str) -> int:
        row = next(r for r in self.deliveries.values() if r["id"] == delivery_id)
        row["status"] = "sending"
        row["attempts"] += 1
        return row["attempts"]

    async def mark_delivery_sent(self, delivery_id: str, provider_message_id: str) -> None:
        row = next(r for r in self.deliveries.values() if r["id"] == delivery_id)
        row.update(status="sent", provider_message_id=provider_message_id)

    async def mark_delivery_failed(
        self, delivery_id: str, error: str, *, retry_in_seconds: float | None
    ) -> None:
        row = next(r for r in self.deliveries.values() if r["id"] == delivery_id)
        row.update(status="failed", error=error)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_user: str | None = None

    async def notify(self, drop_id: str, subscriber: Subscriber, msg: Any, event: Any) -> None:
        if subscriber.user_id == self.fail_user:
            raise RuntimeError("subscriber failure")
        self.sent.append(subscriber.user_id)


@pytest.fixture
def cfg() -> Config:
    return Config("postgres://unused", "redis://unused", max_concurrency=2)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()
