from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import EVENT_ID, FakeStore, message
from harness.config import ScoringWeights
from harness.embeddings import DeterministicEmbedder, EmbeddingError
from harness.scoring import Capabilities, HypeScorer
from harness.store import DemandSnapshot


def scorer(store: FakeStore, **kwargs) -> HypeScorer:
    return HypeScorer(
        store,
        DeterministicEmbedder(dimension=8),
        weights=ScoringWeights(),
        **kwargs,
    )


async def test_empty_preference_set_fails_closed(store: FakeStore):
    """No preferences means no evidence, and no evidence must not reserve."""
    result = await scorer(store).score(message(), EVENT_ID)

    assert result.score == 0.0
    assert result.missing == ["artist", "demand", "sound", "venue"]


async def test_all_signals_present_sums_to_the_weighted_blend(store: FakeStore):
    store.preference_similarity = 1.0
    store.sound_signature = [0.5] * 12
    store.sound_similarity = 1.0
    store.demand = DemandSnapshot(
        attending=1000, first_seen_at=datetime.now(UTC) - timedelta(hours=2)
    )

    result = await scorer(store).score(message(), EVENT_ID)

    assert result.score == pytest.approx(1.0)
    assert result.missing == []


async def test_absent_capability_is_excluded_rather_than_charged(store: FakeStore):
    """A signal the deployment cannot produce must not penalize every event.

    Nothing populates artists.sound_signature, so its 0.25 weight is unearnable
    by every event equally. Charging for it caps every possible score at 0.75 and
    puts the 0.80 reservation threshold permanently out of reach — verified
    against a live drop that scored exactly 0.750 on a perfect artist and venue
    match. A uniform penalty is indistinguishable from a lower threshold, except
    that it is invisible.
    """
    store.preference_similarity = 1.0
    store.sound_signature = None
    store.demand = DemandSnapshot(
        attending=1000, first_seen_at=datetime.now(UTC) - timedelta(hours=2)
    )

    charged = await scorer(store).score(message(), EVENT_ID)
    excluded = await scorer(store, capabilities=Capabilities(sound=False)).score(
        message(), EVENT_ID
    )

    assert charged.score == pytest.approx(0.75)
    assert charged.score < 0.80, "the bug this guards against"
    assert excluded.score == pytest.approx(1.0)
    assert excluded.score >= 0.80


async def test_failed_signal_still_fails_closed_when_capability_exists(store: FakeStore):
    """The distinction that makes the exclusion safe.

    Sound data exists in this deployment but not for this event, so its absence
    is informative and keeps its weight. Only a globally-absent capability leaves
    the denominator.
    """
    store.preference_similarity = 1.0
    store.sound_signature = None
    store.demand = DemandSnapshot(
        attending=1000, first_seen_at=datetime.now(UTC) - timedelta(hours=2)
    )

    result = await scorer(store, capabilities=Capabilities(sound=True)).score(message(), EVENT_ID)

    assert result.score == pytest.approx(0.75)
    assert "sound" in result.missing


async def test_absent_capability_does_not_rescue_a_bad_match(store: FakeStore):
    """Excluding a weight must not manufacture evidence that was never there."""
    store.preference_similarity = 0.0
    store.sound_signature = None
    store.demand = DemandSnapshot(attending=None, first_seen_at=None)

    result = await scorer(store, capabilities=Capabilities(sound=False)).score(message(), EVENT_ID)

    assert result.score == pytest.approx(0.0)


async def test_missing_signal_degrades_rather_than_renormalizing(store: FakeStore):
    """A perfect artist match alone is 0.40, not 1.0.

    This is the property that keeps a drop with three dead signals out of
    RESERVING: the denominator stays at the full weight, so absent evidence
    costs its weight instead of being redistributed to whatever survived.
    """
    store.preference_similarity = 1.0
    store.sound_signature = None
    store.demand = DemandSnapshot(attending=None, first_seen_at=None)

    result = await scorer(store).score(message(venue=""), EVENT_ID)

    assert result.score == pytest.approx(0.40)
    assert result.components["artist"] == pytest.approx(1.0)
    assert result.components["venue"] is None


async def test_negative_cosine_similarity_clamps_to_zero(store: FakeStore):
    store.preference_similarity = -0.8

    result = await scorer(store).score(message(), EVENT_ID)

    assert result.score == 0.0
    assert result.components["artist"] == 0.0


async def test_score_never_leaves_the_unit_interval(store: FakeStore):
    store.preference_similarity = 5.0
    store.sound_signature = [1.0] * 12
    store.sound_similarity = 5.0
    store.demand = DemandSnapshot(
        attending=10_000_000, first_seen_at=datetime.now(UTC) - timedelta(hours=1)
    )

    result = await scorer(store).score(message(), EVENT_ID)

    assert 0.0 <= result.score <= 1.0


async def test_demand_velocity_saturates_and_floors_elapsed_hours(store: FakeStore):
    store.demand = DemandSnapshot(attending=None, first_seen_at=datetime.now(UTC))

    result = await scorer(store, demand_saturation_per_hour=25.0).score(
        message(attending="50"), EVENT_ID
    )

    # 50 attendees over a floored single hour is twice saturation, capped at 1.
    assert result.components["demand"] == pytest.approx(1.0)


async def test_demand_velocity_is_a_rate_not_a_count(store: FakeStore):
    """100 attending on a show first seen 100 hours ago is one per hour, which
    is a normal sell-through; the same 100 inside an hour is the drop."""
    store.demand = DemandSnapshot(
        attending=None, first_seen_at=datetime.now(UTC) - timedelta(hours=100)
    )

    result = await scorer(store, demand_saturation_per_hour=25.0).score(
        message(attending="100"), EVENT_ID
    )

    assert result.components["demand"] == pytest.approx(1.0 / 25.0)


async def test_embedding_failure_degrades_the_signal_instead_of_raising(store: FakeStore):
    class BrokenEmbedder:
        name = "broken"
        dimension = 8

        async def embed(self, texts):
            raise EmbeddingError("voyage is down")

        async def aclose(self):
            return None

    store.preference_similarity = 1.0
    store.sound_signature = [0.1] * 12
    store.sound_similarity = 1.0

    result = await HypeScorer(store, BrokenEmbedder(), weights=ScoringWeights()).score(
        message(), EVENT_ID
    )

    assert result.components["artist"] is None
    assert result.components["venue"] is None
    assert result.score == pytest.approx(0.25)


async def test_lineup_is_capped_at_the_configured_headliner_count(store: FakeStore):
    calls: list[int] = []

    class CountingEmbedder(DeterministicEmbedder):
        async def embed(self, texts):
            calls.append(len(texts))
            return await super().embed(texts)

    long_lineup = ", ".join(f"artist{i}" for i in range(30))
    await HypeScorer(
        store, CountingEmbedder(dimension=8), weights=ScoringWeights(), max_artists=8
    ).score(message(artists=long_lineup), EVENT_ID)

    assert calls[0] == 8


async def test_artist_affinity_takes_the_best_match_not_the_average(store: FakeStore):
    similarities = iter([0.1, 0.9, 0.2])

    async def best(vector, kinds):
        return next(similarities, None)

    store.best_preference_similarity = best  # type: ignore[method-assign]

    result = await scorer(store).score(message(venue=""), EVENT_ID)

    assert result.components["artist"] == pytest.approx(0.9)
