"""Hype scoring: a weighted blend of four independent signals.

ARCHITECTURE.md §6. The blend exists so that a missing signal degrades the
score rather than erroring the node — one embedding timeout must not take the
whole drop down.

The consequential decision here is what "degrades" means, and it turns on a
distinction that is easy to miss: *why* a signal is missing.

**A signal that failed** — an embedding timeout, an event whose artists we have
never seen — contributes zero to the numerator while the denominator keeps its
full weight. Renormalizing there would let a drop with three dead signals and one
strong artist match score 1.0 and walk into RESERVING on a quarter of the
evidence. Absence of evidence must not become evidence.

**A signal this deployment cannot produce at all** is a different thing, and
treating it the same way is a bug. Nothing populates `artists.sound_signature` —
it needs audio analysis that does not exist in this system — so the 0.25 sound
weight is unearnable by every event equally. Keeping it in the denominator does
not make scoring conservative, it silently caps every possible score at 0.75 and
puts the 0.80 reservation threshold permanently out of reach. That was verified,
not theorized: a perfect artist and venue match on a live drop scored 0.750 and
routed to SKIPPED. A uniform penalty on all events is indistinguishable from
lowering the threshold, except that it is invisible.

So a capability that is globally absent is excluded from the denominator, while a
signal that merely failed for this drop is not. The first is a statement about
the deployment; the second is a statement about the event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .config import ScoringWeights
from .embeddings import Embedder, EmbeddingError
from .log import get
from .messages import DropMessage
from .store import Store

_log = get("harness.scoring")

# 'freeform' preferences are matched alongside the typed ones: a note like
# "warehouse techno, no bottle service" is about both artist and venue, and
# forcing the user to file it under one kind would lose the signal.
_ARTIST_KINDS = ("artist", "freeform")
_VENUE_KINDS = ("venue", "freeform")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Which signals this deployment can produce at all.

    Determined once at startup from the data actually present, not from config,
    because the question is whether the underlying data exists rather than
    whether someone remembered to set a flag.

    Defaults to fully capable so that a caller which does not probe gets the
    strict fail-closed behavior rather than an accidentally permissive one.
    """

    sound: bool = True


@dataclass(frozen=True, slots=True)
class HypeScore:
    score: float
    # Per-signal value in [0,1], or None where the signal was unavailable.
    # Persisted into the transition detail so a surprising score is explainable
    # after the fact rather than only reproducible.
    components: dict[str, float | None]

    @property
    def missing(self) -> list[str]:
        return sorted(name for name, value in self.components.items() if value is None)


class HypeScorer:
    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        *,
        weights: ScoringWeights,
        max_artists: int = 8,
        demand_saturation_per_hour: float = 25.0,
        capabilities: Capabilities | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._weights = weights
        self._max_artists = max_artists
        self._demand_saturation = max(demand_saturation_per_hour, 1e-6)
        self._capabilities = capabilities or Capabilities()

    async def score(
        self, msg: DropMessage, event_id: str, *, now: datetime | None = None
    ) -> HypeScore:
        components: dict[str, float | None] = {
            "artist": await self._artist_affinity(msg),
            # Skipped entirely when the capability is absent: without any
            # sound_signature rows the query can only ever return None, so
            # running it is a round trip that buys nothing.
            "sound": await self._sound_match(event_id) if self._capabilities.sound else None,
            "venue": await self._venue_affinity(msg),
            "demand": await self._demand_velocity(msg, event_id, now or datetime.now(UTC)),
        }

        weights = {
            "artist": self._weights.artist,
            "sound": self._weights.sound,
            "venue": self._weights.venue,
            "demand": self._weights.demand,
        }
        total = sum(weights[name] * (value or 0.0) for name, value in components.items())

        # Only globally-absent capabilities leave the denominator. A signal that
        # failed for this drop keeps its weight, so the score still falls.
        earnable = sum(w for name, w in weights.items() if self._is_capable(name))
        if earnable <= 0:
            return HypeScore(score=0.0, components=components)

        return HypeScore(score=_clamp(total / earnable), components=components)

    def _is_capable(self, signal: str) -> bool:
        return self._capabilities.sound if signal == "sound" else True

    async def _artist_affinity(self, msg: DropMessage) -> float | None:
        """Max similarity over the lineup, capped at the top few billed names.

        Max rather than mean: a bill is a set of independent reasons to go, and
        averaging lets six unknown support acts bury the one headliner the user
        actually cares about.
        """
        names = [name for name in msg.artists[: self._max_artists] if name]
        if not names:
            return None
        try:
            vectors = await self._embedder.embed(names)
        except EmbeddingError as exc:
            _log.warning("artist embedding failed", drop_id=msg.drop_id, err=str(exc))
            return None

        best: float | None = None
        for vector in vectors:
            similarity = await self._store.best_preference_similarity(vector, _ARTIST_KINDS)
            if similarity is None:
                continue
            best = similarity if best is None else max(best, similarity)
        return None if best is None else _clamp(best)

    async def _sound_match(self, event_id: str) -> float | None:
        signature = await self._store.event_sound_signature(event_id)
        if not signature:
            return None
        similarity = await self._store.best_sound_similarity(signature)
        return None if similarity is None else _clamp(similarity)

    async def _venue_affinity(self, msg: DropMessage) -> float | None:
        if not msg.venue:
            return None
        try:
            vectors = await self._embedder.embed([msg.venue])
        except EmbeddingError as exc:
            _log.warning("venue embedding failed", drop_id=msg.drop_id, err=str(exc))
            return None
        if not vectors:
            return None
        similarity = await self._store.best_preference_similarity(vectors[0], _VENUE_KINDS)
        return None if similarity is None else _clamp(similarity)

    async def _demand_velocity(
        self, msg: DropMessage, event_id: str, now: datetime
    ) -> float | None:
        """Average attendee growth per hour since we first saw the event.

        The rate, not the count: 400 attending on a show announced six months
        ago is a normal sell-through, while 400 in an afternoon is the drop we
        exist to catch. The elapsed hours are floored at one so a freshly
        discovered event does not divide its way to infinity, and the rate is
        saturated rather than scaled so one arena show cannot compress every
        club night to zero.
        """
        snapshot = await self._store.demand_snapshot(event_id)
        attending = msg.attending if msg.attending is not None else snapshot.attending
        if attending is None or snapshot.first_seen_at is None:
            return None

        first_seen = snapshot.first_seen_at
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=UTC)
        hours = max((now - first_seen).total_seconds() / 3600.0, 1.0)
        return _clamp((attending / hours) / self._demand_saturation)


def _clamp(value: float) -> float:
    """Cosine similarity is [-1,1] and an anti-correlated preference is not a
    negative endorsement, it is just no match. Clamping keeps the blend inside
    the CHECK constraint on drops.hype_score."""
    return max(0.0, min(1.0, float(value)))
