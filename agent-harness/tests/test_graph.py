"""Whole-graph paths.

These assert the transition sequence recorded in drop_transitions, not just the
final state, because the audit log is the thing the admin panel and any
post-mortem actually read.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import EVENT_ID, FakeBus, FakePage, FakeStore, announced_fields, make_config, message
from harness.browser.reserve import (
    ReservationError,
    ReservationResult,
    ReservationStage,
    SelectorMiss,
)
from harness.browser.selectors import SelectorResolver
from harness.config import TERMINAL_STATES, Deadlines, DropState, ReservationMode
from harness.embeddings import DeterministicEmbedder
from harness.fsm.graph import NODE_STATES, build_graph
from harness.fsm.nodes import NodeDeps
from harness.fsm.state import DropFSMState, DropRun
from harness.healing.repair import HealResult
from harness.messages import parse
from harness.scoring import HypeScorer
from test_fsm_nodes import StubHealer, StubReservation

GRAPH_BUDGET_S = 5.0


def compile_graph(store: FakeStore, bus: FakeBus, *, cfg=None, reservation=None, healer=None):
    configuration = cfg or make_config()
    return build_graph(
        NodeDeps(
            cfg=configuration,
            store=store,
            bus=bus,
            scorer=HypeScorer(
                store, DeterministicEmbedder(dimension=8), weights=configuration.weights
            ),
            reservation=reservation or StubReservation(),
            healer=healer or StubHealer(HealResult(ok=True, selector="#healed")),
        )
    )


async def invoke(graph, msg=None, **run_kwargs) -> DropFSMState:
    run = DropRun(msg=msg or message(), event_id=EVENT_ID, **run_kwargs)
    state: DropFSMState = {"run": run, "state": DropState.INGESTED.value}
    return await asyncio.wait_for(graph.ainvoke(state), timeout=GRAPH_BUDGET_S)


def hot(store: FakeStore) -> None:
    store.preference_similarity = 1.0
    store.sound_signature = [0.5] * 12
    store.sound_similarity = 1.0


def test_every_schema_state_has_exactly_one_node():
    assert set(NODE_STATES.values()) == set(DropState)


async def test_cold_availability_drop_ends_skipped(store: FakeStore, bus: FakeBus):
    store.preference_similarity = 0.0

    await invoke(compile_graph(store, bus))

    assert store.states() == [
        DropState.INGESTED,
        DropState.EVALUATING,
        DropState.SKIPPED,
    ]


async def test_hot_availability_drop_reserves_and_notifies(store: FakeStore, bus: FakeBus):
    hot(store)

    await invoke(compile_graph(store, bus))

    assert store.states() == [
        DropState.INGESTED,
        DropState.EVALUATING,
        DropState.RESERVING,
        DropState.NOTIFIED,
    ]


async def test_hot_announcement_promotes_without_ever_reserving(store: FakeStore, bus: FakeBus):
    hot(store)

    await invoke(compile_graph(store, bus), parse(announced_fields()))

    assert DropState.RESERVING not in store.states()
    assert store.final_state() is DropState.NOTIFIED
    assert len(store.watchlist) == 1


async def test_selector_miss_heals_once_and_notifies(store: FakeStore, bus: FakeBus):
    hot(store)
    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=("#a",),
        page=FakePage(),
    )
    reservation = StubReservation(error=miss)

    await invoke(compile_graph(store, bus, reservation=reservation))

    assert store.states() == [
        DropState.INGESTED,
        DropState.EVALUATING,
        DropState.RESERVING,
        DropState.SELF_HEAL,
        DropState.NOTIFIED,
    ]
    # The graph has no edge back into RESERVING, so the browser stage is
    # entered exactly once no matter what the healer reports.
    assert reservation.calls == 1


async def test_failed_heal_terminates_in_failed(store: FakeStore, bus: FakeBus):
    hot(store)
    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=(),
        page=FakePage(),
    )

    await invoke(
        compile_graph(
            store,
            bus,
            reservation=StubReservation(error=miss),
            healer=StubHealer(HealResult(ok=False, reason="no candidate passed")),
        )
    )

    assert store.final_state() is DropState.FAILED
    assert store.states().count(DropState.SELF_HEAL) == 1


async def test_navigation_failure_skips_self_heal_entirely(store: FakeStore, bus: FakeBus):
    hot(store)

    await invoke(
        compile_graph(store, bus, reservation=StubReservation(error=ReservationError("HTTP 503")))
    )

    assert DropState.SELF_HEAL not in store.states()
    assert store.final_state() is DropState.FAILED


async def test_queue_it_drop_is_alert_only_end_to_end(store: FakeStore, bus: FakeBus):
    """The real reservation stage, configured for assist, driven by a queue_it
    event. No browser is launched, so this also proves the constraint holds on
    a machine with no Playwright browsers installed."""
    hot(store)
    cfg = make_config(reservation_mode=ReservationMode.ASSIST)
    graph = compile_graph(
        store,
        bus,
        cfg=cfg,
        reservation=ReservationStage(
            SelectorResolver(cache=bus, store=store), mode=cfg.reservation_mode
        ),
    )

    await invoke(graph, message(queue_it="true"))

    assert store.final_state() is DropState.NOTIFIED
    assert bus.results[-1]["mode"] == ReservationMode.ALERT.value
    assert bus.results[-1]["forced_alert"] is True


async def test_state_deadline_expiry_routes_to_failed_without_retrying(
    store: FakeStore, bus: FakeBus
):
    hot(store)

    class HangingReservation:
        def __init__(self) -> None:
            self.calls = 0

        async def reserve(self, msg):
            self.calls += 1
            await asyncio.sleep(10)
            return ReservationResult(ok=True, mode=ReservationMode.ALERT, reservation_url="")

    reservation = HangingReservation()
    cfg = make_config(deadlines=Deadlines(reserving_ms=100))

    await invoke(compile_graph(store, bus, cfg=cfg, reservation=reservation))

    assert store.final_state() is DropState.FAILED
    assert reservation.calls == 1
    assert "deadline" in store.transitions[-1].detail["error"]


@pytest.mark.parametrize("change", ["new_ticketed", "tier_available", "returned_to_sale"])
async def test_every_availability_change_can_reach_reserving(
    store: FakeStore, bus: FakeBus, change: str
):
    hot(store)

    await invoke(compile_graph(store, bus), message(change=change))

    assert DropState.RESERVING in store.states()


async def test_every_path_ends_in_a_terminal_state(store: FakeStore, bus: FakeBus):
    for similarity in (0.0, 0.5, 0.7, 1.0):
        fresh = FakeStore()
        fresh.preference_similarity = similarity
        fresh.sound_signature = [0.5] * 12
        fresh.sound_similarity = similarity
        for msg in (message(), parse(announced_fields())):
            await invoke(compile_graph(fresh, FakeBus()), msg)
            assert fresh.final_state() in TERMINAL_STATES
