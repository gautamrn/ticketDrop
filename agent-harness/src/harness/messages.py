"""The ticket:drops message contract.

Field-for-field the map built by ingest-service/internal/stream/publisher.go.
Redis streams are flat and string-valued, so every coercion failure has to be
handled here rather than discovered three nodes later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

CHANGE_ANNOUNCED = "announced"
CHANGE_NEW_TICKETED = "new_ticketed"
CHANGE_TIER_AVAILABLE = "tier_available"
CHANGE_RETURNED_TO_SALE = "returned_to_sale"

# Only these three come from the availability tier and can justify a
# reservation attempt. "announced" means the show exists, not that anything is
# buyable, so it can never reach RESERVING (ARCHITECTURE.md §4).
AVAILABILITY_CHANGES = frozenset(
    {CHANGE_NEW_TICKETED, CHANGE_TIER_AVAILABLE, CHANGE_RETURNED_TO_SALE}
)
VALID_CHANGES = AVAILABILITY_CHANGES | {CHANGE_ANNOUNCED}
VALID_SOURCES = frozenset({"ra", "edmtrain"})


class MalformedMessage(ValueError):
    """A message that cannot be routed. Dead-lettered, never retried."""


@dataclass(frozen=True, slots=True)
class DropMessage:
    drop_id: str
    source: str
    source_event_id: str
    event_url: str
    ticket_link: str
    title: str
    venue: str
    starts_at: datetime | None
    artists: tuple[str, ...]
    attending: int | None
    queue_it: bool
    change: str
    fingerprint: str
    detected_at: datetime
    raw: str

    @property
    def is_discovery(self) -> bool:
        return self.change == CHANGE_ANNOUNCED

    @property
    def deep_link(self) -> str:
        """The URL a human is sent to.

        Returned verbatim. EDMTrain's terms require its event link be displayed
        unmodified (ARCHITECTURE.md §10.1), so no redirect wrapper and no
        tracking parameters are added anywhere in the pipeline.
        """
        return self.ticket_link or self.event_url


def parse(fields: dict[str, str]) -> DropMessage:
    drop_id = _text(fields, "drop_id")
    if not drop_id:
        raise MalformedMessage("missing drop_id")

    source = _text(fields, "source")
    if source not in VALID_SOURCES:
        raise MalformedMessage(f"unknown source {source!r}")

    change = _text(fields, "change")
    if change not in VALID_CHANGES:
        raise MalformedMessage(f"unknown change {change!r} for drop {drop_id}")

    source_event_id = _text(fields, "event_id")
    if not source_event_id:
        raise MalformedMessage(f"missing event_id for drop {drop_id}")

    detected_at = _timestamp(fields, "detected_at")
    if detected_at is None:
        raise MalformedMessage(f"missing or unparseable detected_at for drop {drop_id}")

    return DropMessage(
        drop_id=drop_id,
        source=source,
        source_event_id=source_event_id,
        event_url=_text(fields, "event_url"),
        ticket_link=_text(fields, "ticket_link"),
        title=_text(fields, "title"),
        venue=_text(fields, "venue"),
        starts_at=_timestamp(fields, "starts_at"),
        artists=_artists(_text(fields, "artists")),
        attending=_optional_int(fields, "attending"),
        queue_it=_text(fields, "queue_it").lower() == "true",
        change=change,
        fingerprint=_text(fields, "fingerprint"),
        detected_at=detected_at,
        raw=_text(fields, "raw"),
    )


def _text(fields: dict[str, str], key: str) -> str:
    value = fields.get(key)
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _artists(value: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in value.split(",") if name.strip())


def _optional_int(fields: dict[str, str], key: str) -> int | None:
    """Absent is not zero. EDMTrain has no attendance signal at all, and
    treating that as "nobody is going" would suppress every discovery-tier
    score rather than marking the signal missing."""
    raw = _text(fields, key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _timestamp(fields: dict[str, str], key: str) -> datetime | None:
    raw = _text(fields, key)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
