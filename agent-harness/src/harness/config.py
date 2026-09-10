"""Environment-derived configuration.

Mirrors ingest-service/internal/config: the environment is the only source of
secrets, and a missing credential disables a capability rather than crashing
the process. That is what makes the harness runnable with no API keys at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

VECTOR_DIM_DEFAULT = 1024


class ReservationMode(StrEnum):
    """See ARCHITECTURE.md §10. Ordered by how much the bot does on its own."""

    ALERT = "alert"
    ASSIST = "assist"


class DropState(StrEnum):
    """Mirrors the drop_state enum in db/init/001_schema.sql."""

    INGESTED = "INGESTED"
    EVALUATING = "EVALUATING"
    RESERVING = "RESERVING"
    SELF_HEAL = "SELF_HEAL"
    NOTIFIED = "NOTIFIED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset({DropState.NOTIFIED, DropState.SKIPPED, DropState.FAILED})


@dataclass(frozen=True, slots=True)
class Deadlines:
    """Wall-clock ceilings per state. Expiry routes to FAILED, never a retry.

    These are kill switches, not the latency budget from ARCHITECTURE.md §3.
    The budget (350 ms to evaluate, 2.5 s to reserve) is what we alert on; a
    deadline is where we give up on a stage that is never coming back. Setting
    the deadline at the budget would convert every GC pause and cold connection
    into a FAILED drop, so they are deliberately an order of magnitude apart.
    """

    ingested_ms: int = 5_000
    evaluating_ms: int = 10_000
    reserving_ms: int = 30_000
    self_heal_ms: int = 25_000
    terminal_ms: int = 10_000

    def for_state(self, state: DropState) -> float:
        return {
            DropState.INGESTED: self.ingested_ms,
            DropState.EVALUATING: self.evaluating_ms,
            DropState.RESERVING: self.reserving_ms,
            DropState.SELF_HEAL: self.self_heal_ms,
        }.get(state, self.terminal_ms) / 1000.0


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """ARCHITECTURE.md §6. Must sum to 1.0 for the score to stay in [0,1]."""

    artist: float = 0.40
    sound: float = 0.25
    venue: float = 0.20
    demand: float = 0.15

    def total(self) -> float:
        return self.artist + self.sound + self.venue + self.demand


@dataclass(frozen=True, slots=True)
class Config:
    database_url: str
    redis_url: str

    stream: str = "ticket:drops"
    result_stream: str = "ticket:drops:results"
    result_stream_maxlen: int = 100_000
    consumer_group: str = "tdrop-harness"
    consumer_name: str = "harness-1"

    # XREADGROUP block, and how long an entry may sit unacked in another
    # consumer's PEL before XAUTOCLAIM reaps it (ARCHITECTURE.md §5).
    read_block_ms: int = 5_000
    read_batch: int = 16
    autoclaim_min_idle_ms: int = 60_000
    autoclaim_interval_ms: int = 15_000
    # ARCHITECTURE.md §9 failure 4: a poison message is dead-lettered rather
    # than allowed to crash-loop the whole consumer group.
    max_deliveries: int = 3
    # Idempotency claim TTL. Longer than the sum of every state deadline so a
    # slow-but-alive worker never races its own redelivery.
    claim_ttl_s: int = 900

    max_concurrency: int = 4
    hype_threshold: float = 0.80
    watchlist_promote_threshold: float = 0.65
    # How often the harness heals inert watchlist rows whose event_links match
    # arrived after promotion. Dominated by RESOLVE_INTERVAL_MS (~5 min) on the
    # ingest side; this is the remaining lag after a link exists.
    watchlist_repoint_interval_ms: int = 30_000
    watchlist_repoint_timeout_ms: int = 5_000
    self_heal_max_attempts: int = 1
    reservation_mode: ReservationMode = ReservationMode.ALERT

    embedding_model: str = "voyage-3"
    vector_dim: int = VECTOR_DIM_DEFAULT
    voyage_api_key: str = ""
    voyage_base_url: str = "https://api.voyageai.com/v1"
    embed_timeout_ms: int = 3_000
    # How many lineup artists get embedded. The lineup is unbounded upstream
    # and each name is one round trip's worth of tokens; hype is decided by the
    # headliners, not by the eighth support act.
    max_artists_scored: int = 8
    # Attendee growth per hour that saturates the demand signal at 1.0.
    demand_saturation_per_hour: float = 25.0

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-latest"
    heal_timeout_ms: int = 15_000
    # Cleaned-DOM ceiling handed to the model. ARCHITECTURE.md §7 targets
    # ~40 KB; the hard cap stops a pathological page from becoming a
    # five-figure token bill on a stage that has a 1 s budget.
    heal_max_html_bytes: int = 120_000

    deadlines: Deadlines = field(default_factory=Deadlines)
    weights: ScoringWeights = field(default_factory=ScoringWeights)

    log_level: str = "info"
    log_format: str = "json"

    @property
    def has_voyage_key(self) -> bool:
        return bool(self.voyage_api_key)

    @property
    def has_anthropic_key(self) -> bool:
        return bool(self.anthropic_api_key)


def load(env: dict[str, str] | None = None) -> Config:
    """Build a Config from the environment, loading .env from a parent dir.

    Raises ValueError on a configuration that cannot produce correct behavior
    (unknown reservation mode, weights that do not sum to one). Failing at
    startup is the only place these are cheap to fix.
    """
    if env is None:
        _load_dotenv()
        env = dict(os.environ)

    database_url = _str(env, "DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL is required")

    mode_raw = _str(env, "RESERVATION_MODE", "alert").lower()
    try:
        mode = ReservationMode(mode_raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in ReservationMode)
        raise ValueError(f"RESERVATION_MODE must be one of {valid}, got {mode_raw!r}") from exc

    weights = ScoringWeights(
        artist=_float(env, "HYPE_WEIGHT_ARTIST", 0.40),
        sound=_float(env, "HYPE_WEIGHT_SOUND", 0.25),
        venue=_float(env, "HYPE_WEIGHT_VENUE", 0.20),
        demand=_float(env, "HYPE_WEIGHT_DEMAND", 0.15),
    )
    if abs(weights.total() - 1.0) > 1e-6:
        raise ValueError(f"hype weights must sum to 1.0, got {weights.total()}")

    vector_dim = _int(env, "VECTOR_DIM", VECTOR_DIM_DEFAULT)
    if vector_dim != VECTOR_DIM_DEFAULT:
        # pgvector fixes dimensionality at column creation. A mismatch here is
        # not a degraded score, it is an INSERT that cannot succeed.
        raise ValueError(
            f"VECTOR_DIM={vector_dim} does not match the vector(1024) columns in "
            "db/init/001_schema.sql; changing it requires a migration and reindex"
        )

    return Config(
        database_url=database_url,
        redis_url=_str(env, "REDIS_URL", "redis://localhost:6379/0"),
        stream=_str(env, "REDIS_STREAM", "ticket:drops"),
        result_stream=_str(env, "REDIS_RESULT_STREAM", "ticket:drops:results"),
        result_stream_maxlen=_int(env, "REDIS_STREAM_MAXLEN", 100_000),
        consumer_group=_str(env, "REDIS_CONSUMER_GROUP", "tdrop-harness"),
        consumer_name=_str(env, "HARNESS_CONSUMER_NAME", f"harness-{os.getpid()}"),
        read_block_ms=_int(env, "HARNESS_READ_BLOCK_MS", 5_000),
        read_batch=_int(env, "HARNESS_READ_BATCH", 16),
        autoclaim_min_idle_ms=_int(env, "HARNESS_AUTOCLAIM_MIN_IDLE_MS", 60_000),
        autoclaim_interval_ms=_int(env, "HARNESS_AUTOCLAIM_INTERVAL_MS", 15_000),
        max_deliveries=_int(env, "HARNESS_MAX_DELIVERIES", 3),
        claim_ttl_s=_int(env, "HARNESS_CLAIM_TTL_S", 900),
        max_concurrency=_int(env, "HARNESS_MAX_CONCURRENCY", 4),
        hype_threshold=_float(env, "HYPE_THRESHOLD", 0.80),
        watchlist_promote_threshold=_float(env, "WATCHLIST_PROMOTE_THRESHOLD", 0.65),
        watchlist_repoint_interval_ms=max(
            5_000, _int(env, "WATCHLIST_REPOINT_INTERVAL_MS", 30_000)
        ),
        watchlist_repoint_timeout_ms=max(1_000, _int(env, "WATCHLIST_REPOINT_TIMEOUT_MS", 5_000)),
        self_heal_max_attempts=_int(env, "SELF_HEAL_MAX_ATTEMPTS", 1),
        reservation_mode=mode,
        embedding_model=_str(env, "EMBEDDING_MODEL", "voyage-3"),
        vector_dim=vector_dim,
        voyage_api_key=_str(env, "VOYAGE_API_KEY", ""),
        voyage_base_url=_str(env, "VOYAGE_BASE_URL", "https://api.voyageai.com/v1").rstrip("/"),
        embed_timeout_ms=_int(env, "EMBED_TIMEOUT_MS", 3_000),
        max_artists_scored=_int(env, "HYPE_MAX_ARTISTS", 8),
        demand_saturation_per_hour=_float(env, "HYPE_DEMAND_SATURATION_PER_HOUR", 25.0),
        anthropic_api_key=_str(env, "ANTHROPIC_API_KEY", ""),
        anthropic_model=_str(env, "ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
        heal_timeout_ms=_int(env, "SELF_HEAL_TIMEOUT_MS", 15_000),
        heal_max_html_bytes=_int(env, "SELF_HEAL_MAX_HTML_BYTES", 120_000),
        deadlines=Deadlines(
            ingested_ms=_int(env, "DEADLINE_INGESTED_MS", 5_000),
            evaluating_ms=_int(env, "DEADLINE_EVALUATING_MS", 10_000),
            reserving_ms=_int(env, "DEADLINE_RESERVING_MS", 30_000),
            self_heal_ms=_int(env, "DEADLINE_SELF_HEAL_MS", 25_000),
            terminal_ms=_int(env, "DEADLINE_TERMINAL_MS", 10_000),
        ),
        weights=weights,
        log_level=_str(env, "LOG_LEVEL", "info"),
        log_format=_str(env, "LOG_FORMAT", "json"),
    )


def _load_dotenv() -> None:
    """Walk up for a .env so the harness runs from the repo root or its own dir."""
    here = Path.cwd()
    for candidate in [here, *here.parents][:4]:
        env_file = candidate / ".env"
        if env_file.is_file():
            load_dotenv(env_file)
            return


def _str(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key, "").strip()
    return value or default


def _int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(_str(env, key, ""))
    except ValueError:
        return default


def _float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(_str(env, key, ""))
    except ValueError:
        return default
