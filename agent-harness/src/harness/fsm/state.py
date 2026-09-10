"""Graph state.

Split in two on purpose. `DropRun` holds the mutable per-drop working set,
including live handles (a Playwright page) that must not be serialized;
`DropFSMState` is the LangGraph channel dict and carries only scalars, so a
trace or a future checkpointer sees something readable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from ..config import DropState
from ..messages import DropMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..browser.reserve import SelectorMiss
    from ..scoring import HypeScore

# Route labels. Named after the state they lead to so a conditional edge map
# reads as the transition table in ARCHITECTURE.md §4.
ROUTE_EVALUATING = "evaluating"
ROUTE_RESERVING = "reserving"
ROUTE_SELF_HEAL = "self_heal"
ROUTE_NOTIFIED = "notified"
ROUTE_SKIPPED = "skipped"
ROUTE_FAILED = "failed"

# Distinguishes the two ways a drop reaches NOTIFIED: a discovery-tier message
# earns a watchlist slot, an availability-tier message got a reservation.
OUTCOME_PROMOTED = "watchlist_promoted"
OUTCOME_RESERVED = "reserved"
OUTCOME_HEALED = "reserved_after_heal"


@dataclass(slots=True)
class DropRun:
    msg: DropMessage
    event_id: str
    state: DropState = DropState.INGESTED
    stubbed_event: bool = False

    # Monotonic, because latency_ms must survive an NTP correction mid-drop.
    state_entered_at: float = field(default_factory=time.perf_counter)

    hype: HypeScore | None = None
    attempt_count: int = 0
    reservation_url: str = ""
    mode: str = ""
    forced_alert: bool = False
    outcome: str = ""
    healed_selector: str = ""
    miss: SelectorMiss | None = None
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class DropFSMState(TypedDict, total=False):
    run: DropRun
    state: str
    route: str
    hype_score: float
    attempt_count: int
    error: str
