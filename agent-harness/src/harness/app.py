"""Composition root.

Every optional dependency degrades to a keyless implementation here rather
than deeper in the call graph, so there is exactly one place to look to find
out whether a given run is doing real inference or plumbing checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass

from . import embeddings, log
from .browser.reserve import ReservationStage
from .browser.selectors import SelectorResolver
from .bus import RedisBus
from .config import Config
from .fsm.graph import build_graph
from .fsm.nodes import NodeDeps
from .healing import llm as llm_module
from .healing.repair import SelfHealer
from .scoring import Capabilities, HypeScorer
from .store import PgStore
from .worker import Worker

_log = log.get("harness.app")


@dataclass(slots=True)
class Runtime:
    cfg: Config
    store: PgStore
    bus: RedisBus
    embedder: embeddings.Embedder
    llm: llm_module.LLMClient
    worker: Worker

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.llm.aclose()
        await self.bus.aclose()
        await self.store.aclose()


async def build(cfg: Config) -> Runtime:
    store = await PgStore.connect(cfg.database_url)
    bus = RedisBus.from_url(
        cfg.redis_url,
        stream=cfg.stream,
        result_stream=cfg.result_stream,
        result_maxlen=cfg.result_stream_maxlen,
        group=cfg.consumer_group,
        consumer=cfg.consumer_name,
        block_ms=cfg.read_block_ms,
        batch=cfg.read_batch,
        autoclaim_min_idle_ms=cfg.autoclaim_min_idle_ms,
        claim_ttl_s=cfg.claim_ttl_s,
    )
    await bus.ping()

    embedder = embeddings.build(
        api_key=cfg.voyage_api_key,
        model=cfg.embedding_model,
        dimension=cfg.vector_dim,
        base_url=cfg.voyage_base_url,
        timeout_ms=cfg.embed_timeout_ms,
    )
    llm = llm_module.build(
        api_key=cfg.anthropic_api_key,
        model=cfg.anthropic_model,
        timeout_ms=cfg.heal_timeout_ms,
    )

    capabilities = Capabilities(sound=await store.sound_scoring_available())
    if not capabilities.sound:
        _log.warning(
            "no sound-signature data; the sound term is excluded from scoring rather "
            "than charged against every event, which would cap all scores below the "
            "reservation threshold",
            excluded_weight=cfg.weights.sound,
        )

    resolver = SelectorResolver(cache=bus, store=store)
    deps = NodeDeps(
        cfg=cfg,
        store=store,
        bus=bus,
        scorer=HypeScorer(
            store,
            embedder,
            weights=cfg.weights,
            max_artists=cfg.max_artists_scored,
            demand_saturation_per_hour=cfg.demand_saturation_per_hour,
            capabilities=capabilities,
        ),
        reservation=ReservationStage(resolver, mode=cfg.reservation_mode),
        healer=SelfHealer(llm, bus, store, max_html_bytes=cfg.heal_max_html_bytes),
    )

    if await store.count_active_preferences() == 0:
        # Scoring fails closed, so this configuration consumes every message and
        # does nothing, forever, without ever raising. Silence is the failure
        # mode being guarded against here, not a wrong score.
        _log.error(
            "user_preferences is empty: hype scoring can reach at most the demand "
            "weight, which is below both thresholds, so no drop will ever be "
            "reserved or promoted. Seed with: python -m harness prefs add "
            "--kind artist --label '<name>'",
            hype_threshold=cfg.hype_threshold,
            promote_threshold=cfg.watchlist_promote_threshold,
            max_reachable_score=cfg.weights.demand,
        )

    return Runtime(
        cfg=cfg,
        store=store,
        bus=bus,
        embedder=embedder,
        llm=llm,
        worker=Worker(cfg, store, bus, build_graph(deps)),
    )


async def serve(cfg: Config) -> None:
    runtime = await build(cfg)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        await runtime.worker.run(stop)
    finally:
        await runtime.aclose()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # Windows' proactor loop implements neither for SIGTERM; the
            # KeyboardInterrupt path still unwinds the same finally block.
            loop.add_signal_handler(sig, stop.set)
