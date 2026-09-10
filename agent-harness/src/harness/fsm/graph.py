"""The LangGraph StateGraph.

Deliberately acyclic. ARCHITECTURE.md §4 allows SELF_HEAL to resume the
reservation once, but expressing that as an edge back into RESERVING would put
a cycle in the graph and make the one-attempt cap depend entirely on a counter
being correct. Instead SELF_HEAL validates the repaired selector against the
live page and terminates in NOTIFIED or FAILED, so the retry budget is a
property of the graph's shape rather than of its bookkeeping. A topology that
cannot loop cannot starve the worker pool.

Every non-terminal node runs under a wall-clock deadline. Expiry routes to
FAILED and never to a retry: a stage that blew its ceiling is far more likely
to be a hung page or a wedged connection than a transient blip, and retrying
into a hang is how one bad vendor takes the whole pool down.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from ..config import Config, DropState
from ..log import get
from .nodes import NodeDeps, Nodes
from .state import (
    ROUTE_EVALUATING,
    ROUTE_FAILED,
    ROUTE_NOTIFIED,
    ROUTE_RESERVING,
    ROUTE_SELF_HEAL,
    ROUTE_SKIPPED,
    DropFSMState,
)

_log = get("harness.fsm.graph")

NodeFn = Callable[[DropFSMState], Awaitable[DropFSMState]]

# Node name -> the drop_state it occupies. One node per state in the enum, so
# a state that exists in the schema but has no node is a load error, not a
# silent gap.
NODE_STATES: dict[str, DropState] = {
    "ingested": DropState.INGESTED,
    "evaluating": DropState.EVALUATING,
    "reserving": DropState.RESERVING,
    "self_heal": DropState.SELF_HEAL,
    "notified": DropState.NOTIFIED,
    "skipped": DropState.SKIPPED,
    "failed": DropState.FAILED,
}

_TERMINAL_NODES = ("notified", "skipped", "failed")


def build_graph(deps: NodeDeps):
    """Compile the FSM. One graph per process; state is passed per invocation."""
    nodes = Nodes(deps)
    graph: StateGraph = StateGraph(DropFSMState)

    graph.add_node("ingested", _deadline(nodes.ingested, DropState.INGESTED, deps.cfg))
    graph.add_node("evaluating", _deadline(nodes.evaluating, DropState.EVALUATING, deps.cfg))
    graph.add_node("reserving", _deadline(nodes.reserving, DropState.RESERVING, deps.cfg))
    graph.add_node("self_heal", _deadline(nodes.self_heal, DropState.SELF_HEAL, deps.cfg))
    graph.add_node("notified", nodes.notified)
    graph.add_node("skipped", nodes.skipped)
    graph.add_node("failed", nodes.failed)

    graph.add_edge(START, "ingested")
    graph.add_conditional_edges(
        "ingested", _route, {ROUTE_EVALUATING: "evaluating", ROUTE_FAILED: "failed"}
    )
    graph.add_conditional_edges(
        "evaluating",
        _route,
        {
            ROUTE_RESERVING: "reserving",
            ROUTE_SKIPPED: "skipped",
            ROUTE_NOTIFIED: "notified",
            ROUTE_FAILED: "failed",
        },
    )
    graph.add_conditional_edges(
        "reserving",
        _route,
        {
            ROUTE_NOTIFIED: "notified",
            ROUTE_SELF_HEAL: "self_heal",
            ROUTE_FAILED: "failed",
        },
    )
    graph.add_conditional_edges(
        "self_heal", _route, {ROUTE_NOTIFIED: "notified", ROUTE_FAILED: "failed"}
    )
    for terminal in _TERMINAL_NODES:
        graph.add_edge(terminal, END)

    return graph.compile()


def _route(state: DropFSMState) -> str:
    return state.get("route", ROUTE_FAILED)


def _deadline(fn: NodeFn, state: DropState, cfg: Config) -> NodeFn:
    timeout = cfg.deadlines.for_state(state)

    async def wrapped(graph_state: DropFSMState) -> DropFSMState:
        try:
            async with asyncio.timeout(timeout):
                return await fn(graph_state)
        except TimeoutError:
            message = f"{state.value} exceeded its {timeout:.1f}s deadline"
            _log.error(
                "state deadline expired",
                drop_id=graph_state["run"].msg.drop_id,
                state=state.value,
                timeout_s=timeout,
            )
            graph_state["run"].error = message
            return {"state": state.value, "route": ROUTE_FAILED, "error": message}

    return wrapped
