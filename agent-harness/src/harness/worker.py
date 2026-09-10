"""Stream intake, idempotency, and the bounded worker pool.

Everything that makes at-least-once delivery survivable lives here: the claim
key, the duplicate classification, the dead-letter cut-off, and the decision of
when an entry may be acknowledged. The FSM itself assumes it is running exactly
once for a drop, and this module is what earns that assumption.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from .bus import Bus, StreamEntry
from .config import TERMINAL_STATES, Config, DropState
from .fsm.state import DropFSMState, DropRun
from .log import get
from .messages import MalformedMessage, parse
from .store import DropInsert, Store

_log = get("harness.worker")

# Pause after a failed stream read. Short, because the failure is usually a
# reconnect rather than an outage, and long enough not to spin on a dead Redis.
_READ_BACKOFF_S = 1.0


class Worker:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        bus: Bus,
        graph: Any,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._bus = bus
        self._graph = graph
        # ARCHITECTURE.md §9 failure 3: a big drop makes every worker want a
        # browser context at the same instant. The ceiling is the difference
        # between a slow queue and an OOM kill.
        self._slots = asyncio.Semaphore(cfg.max_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        await self._bus.ensure_group()
        _log.info(
            "harness starting",
            group=self._cfg.consumer_group,
            consumer=self._cfg.consumer_name,
            stream=self._cfg.stream,
            mode=self._cfg.reservation_mode.value,
            concurrency=self._cfg.max_concurrency,
        )

        reclaimer = asyncio.create_task(self._reclaim_loop(stop), name="reclaim")
        repointer = asyncio.create_task(self._repoint_loop(stop), name="repoint")
        try:
            while not stop.is_set():
                try:
                    entries = await self._bus.read()
                except Exception as exc:  # noqa: BLE001 - see below
                    # A Redis blip must not end the process. Unacked entries
                    # stay in the PEL and XAUTOCLAIM hands them back, so the
                    # cost of backing off here is latency, not lost drops.
                    _log.error("stream read failed, backing off", err=str(exc))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=_READ_BACKOFF_S)
                    continue
                for entry in entries:
                    await self._spawn(entry)
        finally:
            reclaimer.cancel()
            repointer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reclaimer
            with contextlib.suppress(asyncio.CancelledError):
                await repointer
            await self._drain()

    async def _reclaim_loop(self, stop: asyncio.Event) -> None:
        """XAUTOCLAIM on an interval.

        Separate from the read loop because XREADGROUP blocks: folding the
        reclaim into it would tie recovery latency for a dead consumer's
        backlog to whether new traffic happens to be arriving.
        """
        interval = self._cfg.autoclaim_interval_ms / 1000.0
        while not stop.is_set():
            try:
                for entry in await self._bus.reclaim():
                    _log.warning(
                        "reclaimed abandoned entry",
                        entry_id=entry.entry_id,
                        deliveries=entry.delivery_count,
                    )
                    await self._spawn(entry)
            except Exception as exc:  # noqa: BLE001 - recovery must not die
                _log.error("reclaim failed", err=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    async def _repoint_loop(self, stop: asyncio.Event) -> None:
        """Heal inert watchlist rows once event_links catches up.

        Folding this into the XREADGROUP loop would tie heal latency to drop
        traffic: a quiet night would leave promotions stranded until the next
        announcement. A dedicated loop fires on startup (recovering rows left
        from a previous run) and then on a fixed interval.

        The ingest resolver owns the link and must not write watchlist; this
        loop is the other half of that split. Worst extra detection lag after
        a link exists is one interval (~30 s), which is small against the
        resolver's own ~5 min cadence.
        """
        interval = self._cfg.watchlist_repoint_interval_ms / 1000.0
        timeout = self._cfg.watchlist_repoint_timeout_ms / 1000.0
        while not stop.is_set():
            try:
                moved = await asyncio.wait_for(
                    self._store.repoint_inert_watchlist(), timeout=timeout
                )
                if moved:
                    _log.info("repointed inert watchlist rows", count=moved)
            except TimeoutError:
                _log.error("watchlist repoint timed out", timeout_s=timeout)
            except Exception as exc:  # noqa: BLE001 - recovery must not die
                _log.error("watchlist repoint failed", err=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    async def _spawn(self, entry: StreamEntry) -> None:
        await self._slots.acquire()
        task = asyncio.create_task(self._guarded(entry), name=f"drop:{entry.entry_id}")
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _guarded(self, entry: StreamEntry) -> None:
        try:
            await self.handle(entry)
        except Exception as exc:  # noqa: BLE001 - one bad drop is not an outage
            _log.error("unhandled error processing entry", entry_id=entry.entry_id, err=str(exc))
            await self._bus.ack(entry.entry_id)
        finally:
            self._slots.release()

    async def handle(self, entry: StreamEntry) -> None:
        if entry.delivery_count > self._cfg.max_deliveries:
            # ARCHITECTURE.md §9 failure 4. A message that has survived three
            # workers is not unlucky, it is poison, and leaving it in the PEL
            # blocks the group's recovery path for everything behind it.
            await self._dead_letter(entry, "delivery limit exceeded")
            return

        try:
            msg = parse(entry.fields)
        except MalformedMessage as exc:
            await self._dead_letter(entry, f"malformed message: {exc}")
            return

        # The idempotency gate, taken before any side effect including the
        # INSERT into drops. ARCHITECTURE.md §5.
        if not await self._bus.claim_drop(msg.drop_id):
            await self._handle_claim_miss(entry, msg.drop_id)
            return

        event = await self._store.ensure_event(msg)
        inserted = await self._store.create_drop(msg, event.event_id)

        if inserted is DropInsert.DUPLICATE:
            # The unique index on (event_id, fingerprint) fired: the poll loop
            # reported one availability state twice. Acking is the point of
            # the guard, not a loss.
            _log.info(
                "duplicate drop suppressed",
                drop_id=msg.drop_id,
                event_id=event.event_id,
                fingerprint=msg.fingerprint or "(announced)",
            )
            await self._bus.ack(entry.entry_id)
            return

        if inserted is DropInsert.ALREADY_EXISTS:
            current = await self._store.drop_state(msg.drop_id)
            if current in TERMINAL_STATES:
                _log.info("redelivery of finished drop", drop_id=msg.drop_id, state=current)
                await self._bus.ack(entry.entry_id)
                return
            _log.warning("resuming drop after crash", drop_id=msg.drop_id, state=current)

        run = DropRun(msg=msg, event_id=event.event_id, stubbed_event=event.stubbed)
        state: DropFSMState = {"run": run, "state": DropState.INGESTED.value}
        await self._graph.ainvoke(state)
        await self._bus.ack(entry.entry_id)

    async def _handle_claim_miss(self, entry: StreamEntry, drop_id: str) -> None:
        """Another worker holds the claim. Ack only if the work is provably done.

        Acking unconditionally would silently discard a drop whose owner died
        one instruction after taking the key. Leaving it pending unconditionally
        would spin until the 900 s TTL. Consulting the durable state resolves
        it: a terminal drop is finished and safe to ack, a non-terminal one
        stays in the PEL for XAUTOCLAIM to hand back once the TTL clears.
        """
        current = await self._store.drop_state(drop_id)
        if current in TERMINAL_STATES:
            await self._bus.ack(entry.entry_id)
            return
        _log.info(
            "drop claimed by another worker, leaving pending",
            drop_id=drop_id,
            state=current,
            deliveries=entry.delivery_count,
        )

    async def _dead_letter(self, entry: StreamEntry, reason: str) -> None:
        _log.error("dead-lettering entry", entry_id=entry.entry_id, reason=reason)
        await self._bus.publish_result(
            {
                "entry_id": entry.entry_id,
                "drop_id": entry.fields.get("drop_id", ""),
                "state": DropState.FAILED.value,
                "outcome": "dead_letter",
                "error": reason,
            }
        )
        await self._bus.ack(entry.entry_id)

    async def _drain(self) -> None:
        if not self._inflight:
            return
        _log.info("draining in-flight drops", count=len(self._inflight))
        await asyncio.gather(*list(self._inflight), return_exceptions=True)
