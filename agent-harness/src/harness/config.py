"""Environment-derived configuration for the alert worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv


class DropState(StrEnum):
    NOTIFIED = "NOTIFIED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset(DropState)


@dataclass(frozen=True, slots=True)
class Config:
    database_url: str
    redis_url: str
    stream: str = "ticket:drops"
    consumer_group: str = "tdrop-harness"
    consumer_name: str = "harness-1"
    read_block_ms: int = 5_000
    read_batch: int = 16
    autoclaim_min_idle_ms: int = 60_000
    autoclaim_interval_ms: int = 15_000
    max_deliveries: int = 3
    claim_ttl_s: int = 900
    max_concurrency: int = 4
    subscription_reconcile_interval_ms: int = 15_000
    subscription_reconcile_timeout_ms: int = 5_000
    scout_interval_ms: int = 60_000
    scout_timeout_ms: int = 5_000
    resend_api_key: str = ""
    email_api_base_url: str = "https://api.resend.com"
    alert_from_email: str = "TicketDrop <alerts@example.com>"
    app_base_url: str = "http://localhost:3000"
    unsubscribe_secret: str = "development-only-change-me"
    health_port: int = 2_113
    log_level: str = "info"
    log_format: str = "json"


def load(env: dict[str, str] | None = None) -> Config:
    if env is None:
        _load_dotenv()
        env = dict(os.environ)
    database_url = _str(env, "DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    return Config(
        database_url=database_url,
        redis_url=_str(env, "REDIS_URL", "redis://localhost:6379/0"),
        stream=_str(env, "REDIS_STREAM", "ticket:drops"),
        consumer_group=_str(env, "REDIS_CONSUMER_GROUP", "tdrop-harness"),
        consumer_name=_str(env, "HARNESS_CONSUMER_NAME", f"harness-{os.getpid()}"),
        read_block_ms=_int(env, "HARNESS_READ_BLOCK_MS", 5_000),
        read_batch=_int(env, "HARNESS_READ_BATCH", 16),
        autoclaim_min_idle_ms=_int(env, "HARNESS_AUTOCLAIM_MIN_IDLE_MS", 60_000),
        autoclaim_interval_ms=_int(env, "HARNESS_AUTOCLAIM_INTERVAL_MS", 15_000),
        max_deliveries=_int(env, "HARNESS_MAX_DELIVERIES", 3),
        claim_ttl_s=_int(env, "HARNESS_CLAIM_TTL_S", 900),
        max_concurrency=_int(env, "HARNESS_MAX_CONCURRENCY", 4),
        subscription_reconcile_interval_ms=max(
            5_000, _int(env, "SUBSCRIPTION_RECONCILE_INTERVAL_MS", 15_000)
        ),
        subscription_reconcile_timeout_ms=max(
            1_000, _int(env, "SUBSCRIPTION_RECONCILE_TIMEOUT_MS", 5_000)
        ),
        scout_interval_ms=max(5_000, _int(env, "SCOUT_INTERVAL_MS", 60_000)),
        scout_timeout_ms=max(1_000, _int(env, "SCOUT_TIMEOUT_MS", 5_000)),
        resend_api_key=_str(env, "RESEND_API_KEY", ""),
        email_api_base_url=_str(env, "EMAIL_API_BASE_URL", "https://api.resend.com").rstrip("/"),
        alert_from_email=_str(env, "ALERT_FROM_EMAIL", "TicketDrop <alerts@example.com>"),
        app_base_url=_str(env, "APP_BASE_URL", "http://localhost:3000").rstrip("/"),
        unsubscribe_secret=_str(env, "UNSUBSCRIBE_SECRET", "development-only-change-me"),
        health_port=_int(env, "HARNESS_HEALTH_PORT", 2_113),
        log_level=_str(env, "LOG_LEVEL", "info"),
        log_format=_str(env, "LOG_FORMAT", "json"),
    )


def _load_dotenv() -> None:
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
