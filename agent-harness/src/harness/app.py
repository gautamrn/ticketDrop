"""Harness composition root."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass

from .bus import RedisBus
from .config import Config
from .health import HealthServer
from .notify.email import EmailClient, EmailNotifier, LogEmailClient, ResendClient
from .scout import run_reconciler, run_scout
from .store import PgStore
from .worker import Worker


@dataclass(slots=True)
class Runtime:
    cfg: Config
    store: PgStore
    bus: RedisBus
    email_client: EmailClient
    worker: Worker

    async def aclose(self) -> None:
        await self.email_client.aclose()
        await self.bus.aclose()
        await self.store.aclose()


async def build(cfg: Config) -> Runtime:
    store = await PgStore.connect(cfg.database_url)
    bus = RedisBus.from_url(
        cfg.redis_url,
        stream=cfg.stream,
        group=cfg.consumer_group,
        consumer=cfg.consumer_name,
        block_ms=cfg.read_block_ms,
        batch=cfg.read_batch,
        autoclaim_min_idle_ms=cfg.autoclaim_min_idle_ms,
        claim_ttl_s=cfg.claim_ttl_s,
    )
    await bus.ping()
    email_client: EmailClient = (
        ResendClient(
            cfg.resend_api_key,
            cfg.alert_from_email,
            base_url=cfg.email_api_base_url,
        )
        if cfg.resend_api_key
        else LogEmailClient()
    )
    notifier = EmailNotifier(
        store=store,
        client=email_client,
        app_base_url=cfg.app_base_url,
        unsubscribe_secret=cfg.unsubscribe_secret,
    )
    return Runtime(cfg, store, bus, email_client, Worker(cfg, store, bus, notifier))


async def serve(cfg: Config) -> None:
    runtime = await build(cfg)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    health = HealthServer(cfg.health_port, lambda: _ready(runtime))
    await health.start()
    maintenance = [
        asyncio.create_task(run_scout(cfg, runtime.store, stop), name="scout"),
        asyncio.create_task(run_reconciler(cfg, runtime.store, stop), name="subscriptions"),
    ]
    try:
        await runtime.worker.run(stop)
    finally:
        for task in maintenance:
            task.cancel()
        for task in maintenance:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await health.aclose()
        await runtime.aclose()


async def _ready(runtime: Runtime) -> None:
    await runtime.store.ping()
    await runtime.bus.ping()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)
