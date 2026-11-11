"""Catalogue-to-subscription projection loops."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from .config import Config
from .log import get
from .store import PgStore

_log = get("harness.scout")


async def run_scout(cfg: Config, store: PgStore, stop: asyncio.Event) -> None:
    await _run_loop(
        stop,
        interval_ms=cfg.scout_interval_ms,
        timeout_ms=cfg.scout_timeout_ms,
        operation=store.scout_subscriptions,
        name="scout",
    )


async def run_reconciler(cfg: Config, store: PgStore, stop: asyncio.Event) -> None:
    await _run_loop(
        stop,
        interval_ms=cfg.subscription_reconcile_interval_ms,
        timeout_ms=cfg.subscription_reconcile_timeout_ms,
        operation=store.reconcile_subscriptions,
        name="subscription reconcile",
    )


async def _run_loop(
    stop: asyncio.Event,
    *,
    interval_ms: int,
    timeout_ms: int,
    operation: Callable[[], Awaitable[int]],
    name: str,
) -> None:
    while not stop.is_set():
        try:
            changed = await asyncio.wait_for(operation(), timeout=timeout_ms / 1000)
            if changed:
                _log.info(f"{name} changed rows", count=changed)
        except TimeoutError:
            _log.error(f"{name} timed out", timeout_ms=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - maintenance must survive
            _log.error(f"{name} failed", err=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_ms / 1000)
