from __future__ import annotations

from conftest import FakeNotifier, stream_fields
from harness.bus import StreamEntry
from harness.config import DropState
from harness.store import DropInsert, EventRef, Subscriber
from harness.worker import Worker


async def test_no_subscribers_skips_and_acks(cfg, store, bus) -> None:
    worker = Worker(cfg, store, bus, FakeNotifier())
    await worker.handle(StreamEntry("1-0", stream_fields()))
    assert store.finished[0][1] is DropState.SKIPPED
    assert bus.acked == ["1-0"]


async def test_fanout_isolates_subscriber_failure(cfg, store, bus) -> None:
    store.subscriber_rows = [
        Subscriber("u1", "c1", "one@example.com"),
        Subscriber("u2", "c2", "two@example.com"),
    ]
    notifier = FakeNotifier()
    notifier.fail_user = "u1"
    worker = Worker(cfg, store, bus, notifier)
    await worker.handle(StreamEntry("1-0", stream_fields()))
    assert notifier.sent == ["u2"]
    assert store.finished[0][1] is DropState.NOTIFIED
    assert bus.acked == ["1-0"]


async def test_transition_records_stub_and_change(cfg, store, bus) -> None:
    store.event_ref = EventRef(store.event_ref.event_id, True)
    await Worker(cfg, store, bus, FakeNotifier()).handle(StreamEntry("1-0", stream_fields()))
    detail = store.finished[0][3]
    assert detail["stubbed_event"] is True
    assert detail["change"] == "new_ticketed"


async def test_duplicate_fingerprint_acks_without_fanout(cfg, store, bus) -> None:
    store.insert_result = DropInsert.DUPLICATE
    await Worker(cfg, store, bus, FakeNotifier()).handle(StreamEntry("1-0", stream_fields()))
    assert bus.acked == ["1-0"]
    assert store.finished == []


async def test_claim_miss_leaves_nonterminal_pending(cfg, store, bus) -> None:
    bus.claim_succeeds = False
    await Worker(cfg, store, bus, FakeNotifier()).handle(StreamEntry("1-0", stream_fields()))
    assert bus.acked == []


async def test_claim_miss_acks_terminal_drop(cfg, store, bus) -> None:
    bus.claim_succeeds = False
    drop_id = stream_fields()["drop_id"]
    store.states[drop_id] = DropState.NOTIFIED
    await Worker(cfg, store, bus, FakeNotifier()).handle(StreamEntry("1-0", stream_fields()))
    assert bus.acked == ["1-0"]


async def test_third_delivery_dead_letters_existing_drop(cfg, store, bus) -> None:
    await Worker(cfg, store, bus, FakeNotifier()).handle(
        StreamEntry("1-0", stream_fields(), delivery_count=3)
    )
    assert store.dead_letters[0][0] == stream_fields()["drop_id"]
    assert bus.acked == ["1-0"]


async def test_delivery_limit_waits_for_stale_claim_to_expire(cfg, store, bus) -> None:
    bus.claim_succeeds = False
    await Worker(cfg, store, bus, FakeNotifier()).handle(
        StreamEntry("1-0", stream_fields(), delivery_count=3)
    )
    assert store.dead_letters == []
    assert bus.acked == []


async def test_delivery_limit_acks_terminal_drop_without_dead_letter(cfg, store, bus) -> None:
    drop_id = stream_fields()["drop_id"]
    store.states[drop_id] = DropState.NOTIFIED
    await Worker(cfg, store, bus, FakeNotifier()).handle(
        StreamEntry("1-0", stream_fields(), delivery_count=3)
    )
    assert store.dead_letters == []
    assert bus.acked == ["1-0"]


async def test_malformed_message_is_dead_lettered(cfg, store, bus) -> None:
    await Worker(cfg, store, bus, FakeNotifier()).handle(
        StreamEntry("1-0", stream_fields(change="announced"))
    )
    assert store.dead_letters
    assert bus.acked == ["1-0"]
