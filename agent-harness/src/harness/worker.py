"""At-least-once stream worker with durable, idempotent email fan-out."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from uuid import UUID

from .bus import Bus, StreamEntry
from .config import TERMINAL_STATES, Config, DropState
from .log import get
from .messages import MalformedMessage, parse
from .notify.email import EmailNotifier
from .store import DropInsert, Store

_log = get("harness.worker")
_READ_BACKOFF_S = 1.0


class Worker:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        bus: Bus,
        notifier: EmailNotifier,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._bus = bus
        self._notifier = notifier
        self._slots = asyncio.Semaphore(cfg.max_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        await self._bus.ensure_group()
        reclaimer = asyncio.create_task(self._reclaim_loop(stop), name="reclaim")
        try:
            while not stop.is_set():
                try:
                    entries = await self._bus.read()
                except Exception as exc:  # noqa: BLE001
                    _log.error("stream read failed, backing off", err=str(exc))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=_READ_BACKOFF_S)
                    continue
                for entry in entries:
                    await self._spawn(entry)
        finally:
            reclaimer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reclaimer
            if self._inflight:
                await asyncio.gather(*self._inflight, return_exceptions=True)

    async def _reclaim_loop(self, stop: asyncio.Event) -> None:
        interval = self._cfg.autoclaim_interval_ms / 1000
        while not stop.is_set():
            try:
                for entry in await self._bus.reclaim():
                    await self._spawn(entry)
            except Exception as exc:  # noqa: BLE001
                _log.error("reclaim failed", err=str(exc))
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
        except Exception as exc:  # noqa: BLE001
            # Leave the entry pending. XAUTOCLAIM retries it, and the delivery
            # ceiling eventually turns a poison entry into a durable failure.
            _log.error(
                "unhandled error processing entry",
                entry_id=entry.entry_id,
                err=str(exc),
            )
        finally:
            self._slots.release()

    async def handle(self, entry: StreamEntry) -> None:
        try:
            msg = parse(entry.fields)
        except MalformedMessage as exc:
            await self._dead_letter(entry, f"malformed message: {exc}")
            return

        if not await self._bus.claim_drop(msg.drop_id):
            current = await self._store.drop_state(msg.drop_id)
            if current in TERMINAL_STATES:
                await self._bus.ack(entry.entry_id)
            else:
                _log.info("drop claimed by another worker, leaving pending", drop_id=msg.drop_id)
            return

        current = await self._store.drop_state(msg.drop_id)
        if current in TERMINAL_STATES:
            await self._bus.ack(entry.entry_id)
            return
        if entry.delivery_count >= self._cfg.max_deliveries:
            await self._dead_letter(entry, "delivery limit exceeded")
            return

        event = await self._store.ensure_event(msg)
        inserted = await self._store.create_drop(msg, event.event_id)
        if inserted is DropInsert.DUPLICATE:
            await self._bus.ack(entry.entry_id)
            return
        if inserted is DropInsert.ALREADY_EXISTS:
            current = await self._store.drop_state(msg.drop_id)
            if current in TERMINAL_STATES:
                await self._bus.ack(entry.entry_id)
                return

        subscribers = await self._store.subscribers(event.event_id)
        queue_lag_ms = max(0, int((datetime.now(UTC) - msg.detected_at).total_seconds() * 1000))
        detail = {
            "stubbed_event": event.stubbed,
            "change": msg.change,
            "source_event_id": msg.source_event_id,
        }
        if not subscribers:
            await self._store.finish_drop(msg.drop_id, DropState.SKIPPED, queue_lag_ms, detail)
            await self._bus.ack(entry.entry_id)
            return

        alert = await self._store.alert_context(event.event_id)
        results = await asyncio.gather(
            *(
                self._notifier.notify(msg.drop_id, subscriber, msg, alert)
                for subscriber in subscribers
            ),
            return_exceptions=True,
        )
        for subscriber, result in zip(subscribers, results, strict=True):
            if isinstance(result, BaseException):
                _log.error(
                    "subscriber fan-out failed",
                    drop_id=msg.drop_id,
                    user_id=subscriber.user_id,
                    err=str(result),
                )
        await self._store.finish_drop(msg.drop_id, DropState.NOTIFIED, queue_lag_ms, detail)
        await self._bus.ack(entry.entry_id)

    async def _dead_letter(self, entry: StreamEntry, reason: str) -> None:
        drop_id = entry.fields.get("drop_id", "")
        try:
            UUID(drop_id)
        except ValueError:
            drop_id = ""
        if drop_id:
            with contextlib.suppress(Exception):
                await self._store.dead_letter(drop_id, reason)
        _log.error("dead-lettering entry", entry_id=entry.entry_id, reason=reason)
        await self._bus.ack(entry.entry_id)
