"""FSM node bodies.

Every node's first act is to record its own entry into `drop_transitions`
before doing any work. That ordering is invariant 3 in ARCHITECTURE.md §4: a
worker that dies mid-stage leaves a durable record of the last state it
actually reached, so recovery does not have to guess. The consequence is that
`latency_ms` on a transition row is the time spent in the *previous* state —
the row says "we left EVALUATING for RESERVING after 210 ms", which is exactly
the per-stage number the latency budget in §3 is written against.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..browser.reserve import (
    ReservationError,
    ReservationStage,
    SelectorMiss,
)
from ..browser.selectors import BUY_BUTTON
from ..bus import Bus
from ..config import Config, DropState
from ..healing.repair import SelfHealer
from ..log import get
from ..messages import AVAILABILITY_CHANGES
from ..scoring import HypeScorer
from ..store import AVAILABILITY_SOURCES, Store
from .state import (
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

_log = get("harness.fsm")


@dataclass(slots=True)
class NodeDeps:
    cfg: Config
    store: Store
    bus: Bus
    scorer: HypeScorer
    reservation: ReservationStage
    healer: SelfHealer


class Nodes:
    def __init__(self, deps: NodeDeps) -> None:
        self._d = deps

    # ------------------------------------------------------------- INGESTED

    async def ingested(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        # The one transition whose latency is not our own: it measures the gap
        # between the timestamp Go minted and the moment we picked the message
        # up, which is the queue lag the whole 50 ms ingest budget defends.
        lag_ms = _ms_since(run.msg.detected_at)
        await self._d.store.record_transition(
            run.msg.drop_id,
            None,
            DropState.INGESTED,
            lag_ms,
            {
                "source": run.msg.source,
                "change": run.msg.change,
                "queue_lag_ms": lag_ms,
                "stubbed_event": run.stubbed_event,
            },
        )
        run.state = DropState.INGESTED
        run.state_entered_at = time.perf_counter()
        return {"state": DropState.INGESTED.value, "route": ROUTE_EVALUATING}

    # ----------------------------------------------------------- EVALUATING

    async def evaluating(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        await self._enter(run, DropState.EVALUATING)

        hype = await self._d.scorer.score(run.msg, run.event_id)
        run.hype = hype
        await self._d.store.update_drop(run.msg.drop_id, hype_score=hype.score)

        route = self._route_after_scoring(run, hype.score)
        _log.info(
            "hype scored",
            drop_id=run.msg.drop_id,
            change=run.msg.change,
            score=round(hype.score, 3),
            missing_signals=hype.missing,
            route=route,
        )
        run.detail["hype"] = {"score": round(hype.score, 3), "components": hype.components}
        return {
            "state": DropState.EVALUATING.value,
            "route": route,
            "hype_score": hype.score,
        }

    def _route_after_scoring(self, run: DropRun, score: float) -> str:
        """The branch that keeps discovery messages out of the browser.

        An `announced` message says a show exists, not that anything is
        buyable — EDMTrain has no inventory concept at all. Sending it to
        RESERVING would launch a browser at a listings page and self-heal
        against a control that was never there. Above threshold it buys a
        fast-poll slot instead; that is the entire value of the discovery tier.
        """
        if run.msg.is_discovery:
            if score >= self._d.cfg.watchlist_promote_threshold:
                run.outcome = OUTCOME_PROMOTED
                return ROUTE_NOTIFIED
            return ROUTE_SKIPPED

        if run.msg.change in AVAILABILITY_CHANGES and score >= self._d.cfg.hype_threshold:
            return ROUTE_RESERVING
        return ROUTE_SKIPPED

    # ------------------------------------------------------------ RESERVING

    async def reserving(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        await self._enter(run, DropState.RESERVING)

        try:
            result = await self._d.reservation.reserve(run.msg)
        except SelectorMiss as miss:
            run.miss = miss
            # Invariant 1: SELF_HEAL at most once. Checked here rather than
            # only inside the heal node so the FSM never records an entry into
            # a state it is not allowed to occupy.
            if run.attempt_count < self._d.cfg.self_heal_max_attempts:
                _log.warning(
                    "selector miss, routing to self-heal",
                    drop_id=run.msg.drop_id,
                    logical_name=miss.logical_name,
                    domain=miss.domain,
                    tried=len(miss.tried),
                )
                return {"state": DropState.RESERVING.value, "route": ROUTE_SELF_HEAL}
            run.error = f"selector miss for {miss.logical_name} and heal budget spent"
            await _close_miss(miss)
            return {"state": DropState.RESERVING.value, "route": ROUTE_FAILED, "error": run.error}
        except ReservationError as exc:
            # Navigation faults and 4xx/5xx responses are not selector
            # problems, so they never reach the LLM (ARCHITECTURE.md §7).
            run.error = str(exc)
            return {"state": DropState.RESERVING.value, "route": ROUTE_FAILED, "error": run.error}

        run.reservation_url = result.reservation_url
        run.mode = result.mode.value
        run.forced_alert = result.forced_alert
        run.outcome = OUTCOME_RESERVED
        run.detail["reservation"] = result.detail
        return {"state": DropState.RESERVING.value, "route": ROUTE_NOTIFIED}

    # ------------------------------------------------------------ SELF_HEAL

    async def self_heal(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        await self._enter(run, DropState.SELF_HEAL)

        run.attempt_count += 1
        # Persisted immediately, before the LLM call: if this worker dies mid
        # heal, the redelivered drop must find the budget already spent.
        await self._d.store.update_drop(run.msg.drop_id, attempt_count=run.attempt_count)

        miss = run.miss
        if miss is None or miss.page is None:
            run.error = "self-heal entered without a live page"
            return {"state": DropState.SELF_HEAL.value, "route": ROUTE_FAILED, "error": run.error}

        try:
            result = await self._d.healer.heal(
                page=miss.page,
                domain=miss.domain,
                logical_name=miss.logical_name or BUY_BUTTON,
                url=miss.url,
            )
        except Exception:
            await _close_miss(miss)
            raise
        run.detail["heal"] = {
            "ok": result.ok,
            "selector": result.selector,
            "confidence": result.confidence,
            "reason": result.reason,
            "original_bytes": result.original_bytes,
            "cleaned_bytes": result.cleaned_bytes,
        }
        if not result.ok:
            await _close_miss(miss)
            run.error = f"self-heal failed: {result.reason}"
            return {
                "state": DropState.SELF_HEAL.value,
                "route": ROUTE_FAILED,
                "error": run.error,
                "attempt_count": run.attempt_count,
            }

        run.healed_selector = result.selector
        run.reservation_url = miss.url
        run.outcome = OUTCOME_HEALED
        return {
            "state": DropState.SELF_HEAL.value,
            "route": ROUTE_NOTIFIED,
            "attempt_count": run.attempt_count,
        }

    # ------------------------------------------------------- terminal nodes

    async def notified(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        await self._enter(run, DropState.NOTIFIED)

        if run.outcome == OUTCOME_PROMOTED:
            await self._promote(run)

        await self._d.store.update_drop(
            run.msg.drop_id,
            reservation_url=run.reservation_url or run.msg.deep_link,
            notified=True,
        )
        await self._publish(run, DropState.NOTIFIED)
        _log.info(
            "drop notified",
            drop_id=run.msg.drop_id,
            outcome=run.outcome,
            title=run.msg.title,
            link=run.msg.deep_link,
            mode=run.mode,
            forced_alert=run.forced_alert,
        )
        return {"state": DropState.NOTIFIED.value}

    async def skipped(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        await self._enter(run, DropState.SKIPPED)
        await self._publish(run, DropState.SKIPPED)
        return {"state": DropState.SKIPPED.value}

    async def failed(self, state: DropFSMState) -> DropFSMState:
        run = state["run"]
        run.error = run.error or state.get("error", "") or "unspecified failure"
        await self._enter(run, DropState.FAILED, {"error": run.error})
        await self._d.store.update_drop(run.msg.drop_id, error=run.error)
        await self._publish(run, DropState.FAILED)
        _log.error("drop failed", drop_id=run.msg.drop_id, err=run.error)
        return {"state": DropState.FAILED.value, "error": run.error}

    # ---------------------------------------------------------------- shared

    async def _enter(
        self, run: DropRun, to_state: DropState, detail: dict[str, Any] | None = None
    ) -> None:
        now = time.perf_counter()
        latency_ms = int(max(0.0, (now - run.state_entered_at) * 1000.0))
        await self._d.store.record_transition(
            run.msg.drop_id, run.state, to_state, latency_ms, detail
        )
        await self._d.store.update_drop(run.msg.drop_id, state=to_state)
        run.state = to_state
        run.state_entered_at = now

    async def _promote(self, run: DropRun) -> None:
        """Give the announced show a hot watchlist slot.

        The slot has to land on an event the availability tier can actually
        poll. EDMTrain discovers shows but has no inventory concept, and the
        ingest service's due-items query only returns a watchlist row whose
        event belongs to `watch_source` — so promoting the EDMTrain row under
        'edmtrain' would create a row nothing ever reads. The cross-source link
        written by the ingest resolver into `event_links` is what redirects the
        promotion onto the RA counterpart.

        Falling back to the discovering event when no link exists yet is
        deliberate: the resolver runs off the hot path and may not have matched
        the show at announcement time. The row records the decision and the
        reason says so, which is recoverable, whereas discarding the promotion
        would lose it silently.
        """
        score = run.hype.score if run.hype else 0.0
        target = await self._d.store.availability_watch_target(run.event_id)

        if target is not None:
            await self._d.store.promote_to_watchlist(
                target.event_id,
                watch_source=target.source,
                watch_url=target.url or run.msg.deep_link,
                reason=f"hype {score:.3f} on {run.msg.change} via {run.msg.source}",
            )
            return

        if run.msg.source not in AVAILABILITY_SOURCES:
            _log.warning(
                "promoted an event with no availability-tier counterpart yet",
                drop_id=run.msg.drop_id,
                event_id=run.event_id,
                source=run.msg.source,
            )
        await self._d.store.promote_to_watchlist(
            run.event_id,
            watch_source=run.msg.source,
            watch_url=run.msg.deep_link,
            reason=f"hype {score:.3f} on {run.msg.change}; awaiting event_links resolution",
        )

    async def _publish(self, run: DropRun, state: DropState) -> None:
        await self._d.bus.publish_result(
            {
                "drop_id": run.msg.drop_id,
                "event_id": run.event_id,
                "source": run.msg.source,
                "change": run.msg.change,
                "state": state.value,
                "outcome": run.outcome,
                "hype_score": f"{run.hype.score:.3f}" if run.hype else None,
                "hype_components": run.hype.components if run.hype else None,
                "title": run.msg.title,
                "venue": run.msg.venue,
                "link": run.msg.deep_link,
                "reservation_url": run.reservation_url,
                "mode": run.mode,
                "forced_alert": run.forced_alert,
                "queue_it": run.msg.queue_it,
                "attempt_count": run.attempt_count,
                "healed_selector": run.healed_selector,
                "error": run.error,
                "at": datetime.now(UTC).isoformat(),
            }
        )


def _ms_since(moment: datetime) -> int:
    """Clamped at zero: a message minted a millisecond into the future by a
    skewed clock is a measurement problem, not a negative latency."""
    delta = (datetime.now(UTC) - moment).total_seconds() * 1000.0
    return int(max(0.0, delta))


async def _close_miss(miss: SelectorMiss) -> None:
    if miss.cleanup is not None:
        with contextlib.suppress(Exception):
            await miss.cleanup()
