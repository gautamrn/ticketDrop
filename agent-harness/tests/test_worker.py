"""Intake semantics: idempotency, dedupe, poison messages, and acking.

The FSM assumes exactly-once. These tests are what that assumption rests on.
"""

from __future__ import annotations

import asyncio

from conftest import FakeBus, FakeStore, make_config, stream_fields
from harness.bus import StreamEntry
from harness.config import DropState
from harness.store import DropInsert
from harness.worker import Worker


class RecordingGraph:
    def __init__(self) -> None:
        self.invocations = 0

    async def ainvoke(self, state):
        self.invocations += 1
        return state


def worker(store: FakeStore, bus: FakeBus, graph, **cfg_overrides) -> Worker:
    return Worker(make_config(**cfg_overrides), store, bus, graph)


def entry(entry_id: str = "1-0", deliveries: int = 1, **overrides: str) -> StreamEntry:
    return StreamEntry(
        entry_id=entry_id, fields=stream_fields(**overrides), delivery_count=deliveries
    )


async def test_a_normal_message_runs_the_graph_once_and_acks(store: FakeStore, bus: FakeBus):
    graph = RecordingGraph()

    await worker(store, bus, graph).handle(entry())

    assert graph.invocations == 1
    assert bus.acked == ["1-0"]


async def test_the_claim_is_taken_before_any_write(store: FakeStore, bus: FakeBus):
    order: list[str] = []
    original_claim = bus.claim_drop
    original_ensure = store.ensure_event

    async def claim(drop_id: str) -> bool:
        order.append("claim")
        return await original_claim(drop_id)

    async def ensure(msg):
        order.append("ensure_event")
        return await original_ensure(msg)

    bus.claim_drop = claim  # type: ignore[method-assign]
    store.ensure_event = ensure  # type: ignore[method-assign]

    await worker(store, bus, RecordingGraph()).handle(entry())

    assert order == ["claim", "ensure_event"]


async def test_redelivery_while_another_worker_holds_the_claim_is_left_pending(
    store: FakeStore, bus: FakeBus
):
    """Not acked: the owner may have died one instruction after taking the key,
    and acking here would silently lose the drop."""
    graph = RecordingGraph()
    bus.claim_succeeds = False
    store.drops["0192f0c0-0000-7000-8000-000000000001"] = DropState.RESERVING

    await worker(store, bus, graph).handle(entry())

    assert graph.invocations == 0
    assert bus.acked == []


async def test_redelivery_of_a_finished_drop_is_acked(store: FakeStore, bus: FakeBus):
    graph = RecordingGraph()
    bus.claim_succeeds = False
    store.drops["0192f0c0-0000-7000-8000-000000000001"] = DropState.NOTIFIED

    await worker(store, bus, graph).handle(entry())

    assert graph.invocations == 0
    assert bus.acked == ["1-0"]


async def test_duplicate_fingerprint_is_suppressed_not_reprocessed(store: FakeStore, bus: FakeBus):
    """The unique index on (event_id, fingerprint) is the dedupe guard that
    stops a 2 s poll loop from emitting one change thirty times a minute."""
    graph = RecordingGraph()
    store.insert_result = DropInsert.DUPLICATE

    await worker(store, bus, graph).handle(entry())

    assert graph.invocations == 0
    assert bus.acked == ["1-0"]


async def test_crash_recovery_resumes_a_non_terminal_drop(store: FakeStore, bus: FakeBus):
    graph = RecordingGraph()
    store.insert_result = DropInsert.ALREADY_EXISTS
    store.drops["0192f0c0-0000-7000-8000-000000000001"] = DropState.EVALUATING

    await worker(store, bus, graph).handle(entry())

    assert graph.invocations == 1
    assert bus.acked == ["1-0"]


async def test_redelivered_terminal_drop_is_not_rerun(store: FakeStore, bus: FakeBus):
    graph = RecordingGraph()
    store.insert_result = DropInsert.ALREADY_EXISTS
    store.drops["0192f0c0-0000-7000-8000-000000000001"] = DropState.FAILED

    await worker(store, bus, graph).handle(entry())

    assert graph.invocations == 0
    assert bus.acked == ["1-0"]


async def test_malformed_message_is_dead_lettered_immediately(store: FakeStore, bus: FakeBus):
    graph = RecordingGraph()

    await worker(store, bus, graph).handle(entry(source="ticketmaster"))

    assert graph.invocations == 0
    assert bus.acked == ["1-0"]
    assert bus.results[-1]["outcome"] == "dead_letter"


async def test_poison_message_is_dead_lettered_after_the_delivery_limit(
    store: FakeStore, bus: FakeBus
):
    """A message that has survived three workers is not unlucky, it is poison,
    and leaving it in the PEL blocks recovery for everything behind it."""
    graph = RecordingGraph()

    await worker(store, bus, graph, max_deliveries=3).handle(entry(deliveries=4))

    assert graph.invocations == 0
    assert bus.acked == ["1-0"]
    assert "delivery limit" in bus.results[-1]["error"]


async def test_an_exploding_graph_acks_rather_than_blocking_the_group(
    store: FakeStore, bus: FakeBus
):
    class ExplodingGraph:
        async def ainvoke(self, state):
            raise RuntimeError("node blew up")

    w = worker(store, bus, ExplodingGraph())
    await w._guarded(entry())

    assert bus.acked == ["1-0"]


async def test_concurrency_ceiling_is_respected(store: FakeStore, bus: FakeBus):
    """ARCHITECTURE.md §9 failure 3: a big drop makes every worker want a
    browser context at the same instant."""
    peak = 0
    active = 0
    release = asyncio.Event()

    class BlockingGraph:
        async def ainvoke(self, state):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return state

    w = worker(store, bus, BlockingGraph(), max_concurrency=2)
    spawns = [
        asyncio.create_task(
            w._spawn(entry(entry_id=f"{i}-0", drop_id=f"0192f0c0-0000-7000-8000-00000000000{i}"))
        )
        for i in range(4)
    ]
    await asyncio.sleep(0.05)

    assert peak == 2

    release.set()
    await asyncio.gather(*spawns)
    await w._drain()
    assert len(bus.acked) == 4


async def test_a_failing_stream_read_backs_off_instead_of_ending_the_process(
    store: FakeStore, bus: FakeBus
):
    """Unacked entries stay in the PEL and XAUTOCLAIM hands them back, so the
    cost of backing off is latency, not lost drops. Exiting loses the worker."""
    attempts = 0

    async def failing_read():
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0)
        raise ConnectionError("connection reset by peer")

    bus.read = failing_read  # type: ignore[method-assign]
    stop = asyncio.Event()
    w = worker(store, bus, RecordingGraph())

    runner = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(runner, timeout=3.0)

    assert attempts >= 1


async def test_run_loop_creates_the_group_and_drains_on_stop(store: FakeStore, bus: FakeBus):
    stop = asyncio.Event()
    w = worker(store, bus, RecordingGraph())

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(w.run(stop), timeout=3.0)
    await stopper

    assert bus.group_created is True


async def test_repoint_loop_runs_on_startup_without_waiting_for_traffic(
    store: FakeStore, bus: FakeBus
):
    """Quiet nights produce no stream entries. Folding the heal into XREAD
    would leave promotions stranded until the next announcement."""
    stop = asyncio.Event()
    w = worker(store, bus, RecordingGraph(), watchlist_repoint_interval_ms=50)

    runner = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.08)
    stop.set()
    await asyncio.wait_for(runner, timeout=3.0)

    assert store.repoint_calls >= 1


async def test_a_failing_repoint_does_not_kill_the_worker(store: FakeStore, bus: FakeBus):
    store.repoint_error = RuntimeError("postgres went away")
    stop = asyncio.Event()
    w = worker(store, bus, RecordingGraph(), watchlist_repoint_interval_ms=50)

    runner = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.08)
    stop.set()
    await asyncio.wait_for(runner, timeout=3.0)

    assert store.repoint_calls >= 1
    assert bus.group_created is True
