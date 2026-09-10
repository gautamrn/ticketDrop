from __future__ import annotations

import pytest

from harness.config import Deadlines, DropState, ReservationMode, ScoringWeights, load

BASE = {"DATABASE_URL": "postgres://ticketdrop@localhost/ticketdrop"}


def test_defaults_match_the_documented_posture():
    cfg = load(dict(BASE))

    assert cfg.reservation_mode is ReservationMode.ALERT
    assert cfg.hype_threshold == 0.80
    assert cfg.watchlist_promote_threshold == 0.65
    assert cfg.watchlist_repoint_interval_ms == 30_000
    assert cfg.self_heal_max_attempts == 1
    assert cfg.consumer_group == "tdrop-harness"


def test_database_url_is_required():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        load({})


def test_unknown_reservation_mode_is_rejected_at_startup():
    with pytest.raises(ValueError, match="RESERVATION_MODE"):
        load(BASE | {"RESERVATION_MODE": "yolo"})


def test_vector_dim_must_match_the_schema():
    """pgvector fixes dimensionality at column creation, so a mismatch is an
    INSERT that cannot succeed, not a slightly worse score."""
    with pytest.raises(ValueError, match="1536"):
        load(BASE | {"VECTOR_DIM": "1536"})


def test_weights_must_sum_to_one_or_the_score_leaves_the_unit_interval():
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        load(BASE | {"HYPE_WEIGHT_ARTIST": "0.9"})


def test_documented_weights_are_the_defaults():
    weights = ScoringWeights()

    assert (weights.artist, weights.sound, weights.venue, weights.demand) == (
        0.40,
        0.25,
        0.20,
        0.15,
    )
    assert weights.total() == pytest.approx(1.0)


def test_deadlines_exceed_the_latency_budget_they_guard():
    """A deadline is a kill switch, not the budget. Setting it at the budget
    would turn every GC pause into a FAILED drop."""
    deadlines = Deadlines()

    assert deadlines.for_state(DropState.EVALUATING) > 0.350
    assert deadlines.for_state(DropState.RESERVING) > 2.5
    assert deadlines.for_state(DropState.NOTIFIED) == deadlines.terminal_ms / 1000


def test_repoint_interval_is_floored_so_a_zero_does_not_spin():
    cfg = load(BASE | {"WATCHLIST_REPOINT_INTERVAL_MS": "0"})

    assert cfg.watchlist_repoint_interval_ms == 5_000


def test_claim_ttl_outlives_the_sum_of_every_state_deadline():
    cfg = load(dict(BASE))
    worst_case = sum(
        cfg.deadlines.for_state(state)
        for state in (
            DropState.INGESTED,
            DropState.EVALUATING,
            DropState.RESERVING,
            DropState.SELF_HEAL,
            DropState.NOTIFIED,
        )
    )

    assert cfg.claim_ttl_s > worst_case
