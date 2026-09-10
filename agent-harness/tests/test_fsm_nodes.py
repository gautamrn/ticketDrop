"""One test per FSM node: what state it records, where it routes, and that it
returns at all.

Termination is asserted explicitly with a wait_for wrapper rather than trusted.
A node that hangs is indistinguishable from a slow one in a passing test suite,
and the whole reason every state carries a deadline is that this pipeline has
no tolerance for a stage that never comes back.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from conftest import EVENT_ID, FakeBus, FakePage, FakeStore, announced_fields, make_config, message
from harness.browser.reserve import (
    ReservationError,
    ReservationResult,
    SelectorMiss,
)
from harness.config import Config, DropState, ReservationMode
from harness.embeddings import DeterministicEmbedder
from harness.fsm.nodes import NodeDeps, Nodes
from harness.fsm.state import (
    OUTCOME_HEALED,
    OUTCOME_PROMOTED,
    OUTCOME_RESERVED,
    ROUTE_EVALUATING,
    ROUTE_FAILED,
    ROUTE_NOTIFIED,
    ROUTE_RESERVING,
    ROUTE_SELF_HEAL,
    ROUTE_SKIPPED,
    DropFSMState,
    DropRun,
)
from harness.healing.repair import HealResult
from harness.messages import parse
from harness.scoring import HypeScorer
from harness.store import WatchTarget

TERMINATION_BUDGET_S = 2.0


class StubReservation:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def reserve(self, msg):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result or ReservationResult(
            ok=True, mode=ReservationMode.ALERT, reservation_url=msg.deep_link
        )


class StubHealer:
    def __init__(self, result: HealResult) -> None:
        self._result = result
        self.calls = 0

    async def heal(self, *, page, domain, logical_name, url) -> HealResult:
        self.calls += 1
        return self._result


def build_nodes(
    store: FakeStore,
    bus: FakeBus,
    *,
    cfg: Config | None = None,
    reservation: Any = None,
    healer: Any = None,
) -> Nodes:
    configuration = cfg or make_config()
    return Nodes(
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


def state_for(msg=None, **run_kwargs: Any) -> DropFSMState:
    run = DropRun(msg=msg or message(), event_id=EVENT_ID, **run_kwargs)
    return {"run": run, "state": DropState.INGESTED.value}


async def run_node(node, state: DropFSMState) -> DropFSMState:
    """Every node must return inside a bounded wall-clock window."""
    return await asyncio.wait_for(node(state), timeout=TERMINATION_BUDGET_S)


# ------------------------------------------------------------------ INGESTED


async def test_ingested_records_the_queue_lag_and_always_evaluates(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)
    state = state_for()

    out = await run_node(nodes.ingested, state)

    assert out["state"] == DropState.INGESTED.value
    assert out["route"] == ROUTE_EVALUATING
    assert store.transitions[0].from_state is None
    assert store.transitions[0].to_state is DropState.INGESTED
    assert store.transitions[0].latency_ms >= 0
    assert "queue_lag_ms" in store.transitions[0].detail


# ---------------------------------------------------------------- EVALUATING


async def test_evaluating_routes_above_threshold_to_reserving(store: FakeStore, bus: FakeBus):
    store.preference_similarity = 1.0
    store.sound_signature = [0.5] * 12
    store.sound_similarity = 1.0
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.evaluating, state_for())

    assert out["state"] == DropState.EVALUATING.value
    assert out["route"] == ROUTE_RESERVING
    assert store.transitions[-1].to_state is DropState.EVALUATING


async def test_evaluating_routes_below_threshold_to_skipped(store: FakeStore, bus: FakeBus):
    store.preference_similarity = 0.1
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.evaluating, state_for())

    assert out["route"] == ROUTE_SKIPPED


async def test_evaluating_persists_the_score_before_routing(store: FakeStore, bus: FakeBus):
    store.preference_similarity = 0.5
    nodes = build_nodes(store, bus)

    await run_node(nodes.evaluating, state_for())

    assert any("hype_score" in update for update in store.updates)


@pytest.mark.parametrize("score", [0.0, 0.64, 0.649])
async def test_announced_below_promote_threshold_is_skipped(
    store: FakeStore, bus: FakeBus, score: float
):
    store.preference_similarity = score
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.evaluating, state_for(parse(announced_fields())))

    assert out["route"] == ROUTE_SKIPPED


async def test_announced_above_promote_threshold_goes_to_notified_not_reserving(
    store: FakeStore, bus: FakeBus
):
    """The discovery branch. An announcement has nothing to reserve — EDMTrain
    does not know whether tickets are on sale — so it buys a watchlist slot."""
    store.preference_similarity = 1.0
    store.sound_signature = [0.5] * 12
    store.sound_similarity = 1.0
    nodes = build_nodes(store, bus)
    state = state_for(parse(announced_fields()))

    out = await run_node(nodes.evaluating, state)

    assert out["route"] == ROUTE_NOTIFIED
    assert state["run"].outcome == OUTCOME_PROMOTED


async def test_announced_message_can_never_route_to_reserving(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)
    for similarity in (0.0, 0.5, 0.66, 0.9, 1.0):
        store.preference_similarity = similarity
        store.sound_signature = [0.5] * 12
        store.sound_similarity = similarity
        out = await run_node(nodes.evaluating, state_for(parse(announced_fields())))
        assert out["route"] != ROUTE_RESERVING


# ----------------------------------------------------------------- RESERVING


async def test_reserving_success_routes_to_notified(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)
    state = state_for()

    out = await run_node(nodes.reserving, state)

    assert out["state"] == DropState.RESERVING.value
    assert out["route"] == ROUTE_NOTIFIED
    assert state["run"].outcome == OUTCOME_RESERVED
    assert state["run"].reservation_url == "https://vendor.example/checkout/1234"


async def test_reserving_selector_miss_routes_to_self_heal(store: FakeStore, bus: FakeBus):
    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=("#a",),
        page=FakePage(),
    )
    nodes = build_nodes(store, bus, reservation=StubReservation(error=miss))
    state = state_for()

    out = await run_node(nodes.reserving, state)

    assert out["route"] == ROUTE_SELF_HEAL
    assert state["run"].miss is miss


async def test_reserving_selector_miss_fails_once_the_heal_budget_is_spent(
    store: FakeStore, bus: FakeBus
):
    """Invariant 1 enforced at the routing boundary, so the FSM never even
    records an entry into a state it is not allowed to occupy twice."""
    closed = False

    async def cleanup() -> None:
        nonlocal closed
        closed = True

    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=("#a",),
        page=FakePage(),
        cleanup=cleanup,
    )
    nodes = build_nodes(store, bus, reservation=StubReservation(error=miss))

    out = await run_node(nodes.reserving, state_for(attempt_count=1))

    assert out["route"] == ROUTE_FAILED
    assert DropState.SELF_HEAL not in store.states()
    assert closed is True


async def test_reserving_non_selector_error_never_reaches_self_heal(store: FakeStore, bus: FakeBus):
    """A 503 or an auth wall is not a selector problem; asking an LLM about it
    just burns the latency budget (ARCHITECTURE.md §7)."""
    nodes = build_nodes(
        store, bus, reservation=StubReservation(error=ReservationError("page returned HTTP 503"))
    )

    out = await run_node(nodes.reserving, state_for())

    assert out["route"] == ROUTE_FAILED
    assert "503" in out["error"]


# ----------------------------------------------------------------- SELF_HEAL


async def test_self_heal_success_routes_to_notified(store: FakeStore, bus: FakeBus):
    closed = False

    async def cleanup() -> None:
        nonlocal closed
        closed = True

    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=("#a",),
        page=FakePage(),
        cleanup=cleanup,
    )
    nodes = build_nodes(store, bus, healer=StubHealer(HealResult(ok=True, selector="#healed")))
    state = state_for(miss=miss)

    out = await run_node(nodes.self_heal, state)

    assert out["state"] == DropState.SELF_HEAL.value
    assert out["route"] == ROUTE_NOTIFIED
    assert state["run"].healed_selector == "#healed"
    assert state["run"].outcome == OUTCOME_HEALED
    assert closed is False


async def test_self_heal_spends_the_attempt_before_calling_the_model(
    store: FakeStore, bus: FakeBus
):
    """Persisted first so a worker that dies mid-heal cannot have the budget
    refunded by its own redelivery."""
    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=(),
        page=FakePage(),
    )
    nodes = build_nodes(store, bus)
    state = state_for(miss=miss)

    await run_node(nodes.self_heal, state)

    attempts = [u for u in store.updates if u.get("attempt_count") is not None]
    assert attempts[0]["attempt_count"] == 1
    assert state["run"].attempt_count == 1


async def test_self_heal_failure_routes_to_failed(store: FakeStore, bus: FakeBus):
    closed = False

    async def cleanup() -> None:
        nonlocal closed
        closed = True

    miss = SelectorMiss(
        logical_name="buy_button",
        url="https://vendor.example/x",
        domain="vendor.example",
        tried=(),
        page=FakePage(),
        cleanup=cleanup,
    )
    nodes = build_nodes(
        store, bus, healer=StubHealer(HealResult(ok=False, reason="no candidate passed"))
    )

    out = await run_node(nodes.self_heal, state_for(miss=miss))

    assert out["route"] == ROUTE_FAILED
    assert "no candidate passed" in out["error"]
    assert closed is True


async def test_self_heal_without_a_live_page_fails_rather_than_guessing(
    store: FakeStore, bus: FakeBus
):
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.self_heal, state_for())

    assert out["route"] == ROUTE_FAILED


# ------------------------------------------------------------------ NOTIFIED


async def test_notified_is_terminal_and_publishes_a_result(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.notified, state_for())

    assert out["state"] == DropState.NOTIFIED.value
    assert out.get("route") is None
    assert store.transitions[-1].to_state is DropState.NOTIFIED
    assert bus.results[-1]["state"] == DropState.NOTIFIED.value


async def test_notified_stamps_notified_at_and_the_reservation_url(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)

    await run_node(nodes.notified, state_for())

    final = store.updates[-1]
    assert final["notified"] is True
    assert final["reservation_url"] == "https://vendor.example/checkout/1234"


async def test_notified_promotes_a_discovery_drop_to_the_hot_watchlist(
    store: FakeStore, bus: FakeBus
):
    nodes = build_nodes(store, bus)
    state = state_for(parse(announced_fields()), outcome=OUTCOME_PROMOTED)

    await run_node(nodes.notified, state)

    assert len(store.watchlist) == 1
    assert store.watchlist[0]["event_id"] == EVENT_ID
    assert store.watchlist[0]["watch_source"] == "edmtrain"


async def test_promotion_redirects_onto_the_linked_availability_event(
    store: FakeStore, bus: FakeBus
):
    """EDMTrain finds it, RA watches it.

    The ingest poller only returns a watchlist row whose event belongs to
    watch_source, so a promotion left on the EDMTrain row would never be polled.
    """
    store.watch_target = WatchTarget(
        event_id="22222222-2222-4222-8222-222222222222",
        source="ra",
        url="https://ra.co/events/4321",
    )
    nodes = build_nodes(store, bus)

    await run_node(nodes.notified, state_for(parse(announced_fields()), outcome=OUTCOME_PROMOTED))

    assert store.watchlist[0]["event_id"] == "22222222-2222-4222-8222-222222222222"
    assert store.watchlist[0]["watch_source"] == "ra"
    assert store.watchlist[0]["watch_url"] == "https://ra.co/events/4321"


async def test_unlinked_promotion_still_records_the_decision(store: FakeStore, bus: FakeBus):
    store.watch_target = None
    nodes = build_nodes(store, bus)

    await run_node(nodes.notified, state_for(parse(announced_fields()), outcome=OUTCOME_PROMOTED))

    assert store.watchlist[0]["watch_source"] == "edmtrain"
    assert "awaiting event_links" in store.watchlist[0]["reason"]


async def test_notified_does_not_promote_an_availability_drop(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)

    await run_node(nodes.notified, state_for(outcome=OUTCOME_RESERVED))

    assert store.watchlist == []


# ------------------------------------------------------------------- SKIPPED


async def test_skipped_is_terminal_and_publishes_a_result(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.skipped, state_for())

    assert out["state"] == DropState.SKIPPED.value
    assert out.get("route") is None
    assert store.transitions[-1].to_state is DropState.SKIPPED
    assert bus.results[-1]["state"] == DropState.SKIPPED.value


# -------------------------------------------------------------------- FAILED


async def test_failed_is_terminal_and_records_the_error(store: FakeStore, bus: FakeBus):
    nodes = build_nodes(store, bus)
    state = state_for()
    state["error"] = "page returned HTTP 503"

    out = await run_node(nodes.failed, state)

    assert out["state"] == DropState.FAILED.value
    assert out.get("route") is None
    assert store.transitions[-1].to_state is DropState.FAILED
    assert any(u.get("error") == "page returned HTTP 503" for u in store.updates)
    assert bus.results[-1]["error"] == "page returned HTTP 503"


async def test_failed_always_has_an_error_even_when_none_was_supplied(
    store: FakeStore, bus: FakeBus
):
    nodes = build_nodes(store, bus)

    out = await run_node(nodes.failed, state_for())

    assert out["error"]


# -------------------------------------------------------------- ordering


async def test_transition_is_written_before_the_node_does_its_work(store: FakeStore, bus: FakeBus):
    """Invariant 3: a crashed worker's last known state must be recoverable,
    which only holds if the transition row lands before the stage runs."""
    order: list[str] = []
    original = store.record_transition

    async def tracking(*args, **kwargs):
        order.append("transition")
        await original(*args, **kwargs)

    store.record_transition = tracking  # type: ignore[method-assign]

    class RecordingReservation(StubReservation):
        async def reserve(self, msg):
            order.append("reserve")
            return await super().reserve(msg)

    nodes = build_nodes(store, bus, reservation=RecordingReservation())
    await run_node(nodes.reserving, state_for())

    assert order == ["transition", "reserve"]
