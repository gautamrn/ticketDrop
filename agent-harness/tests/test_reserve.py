from __future__ import annotations

import pytest

from conftest import FakeBus, FakeLocator, FakePage, FakeStore, message
from harness.browser.reserve import (
    ReservationStage,
    SelectorMiss,
    resolve_mode,
)
from harness.browser.selectors import SelectorResolver, domain_of
from harness.config import ReservationMode


def stage(store: FakeStore, bus: FakeBus, mode: ReservationMode) -> ReservationStage:
    return ReservationStage(SelectorResolver(cache=bus, store=store), mode=mode)


# ------------------------------------------------------------------- modes


@pytest.mark.parametrize("configured", [ReservationMode.ALERT, ReservationMode.ASSIST])
def test_queue_it_forces_alert_regardless_of_configured_mode(configured: ReservationMode):
    """ARCHITECTURE.md §10: an architectural constraint, not a runtime preference.

    A Queue-it waiting room carries legal weight (BOTS Act, UK Breaching of
    Limits regulations), so it always forces alert-only behavior.
    """
    mode, forced = resolve_mode(configured, queue_it=True)

    assert mode is ReservationMode.ALERT
    assert forced is (configured is not ReservationMode.ALERT)


def test_non_queue_it_events_keep_the_configured_mode():
    assert resolve_mode(ReservationMode.ASSIST, queue_it=False) == (
        ReservationMode.ASSIST,
        False,
    )


async def test_queue_it_event_never_launches_a_browser(store: FakeStore, bus: FakeBus):
    """The strongest form of the constraint: assist mode plus queue_it still
    takes the no-browser path, so a missing Playwright install cannot even be
    reached for such an event."""
    result = await stage(store, bus, ReservationMode.ASSIST).reserve(message(queue_it="true"))

    assert result.ok is True
    assert result.mode is ReservationMode.ALERT
    assert result.forced_alert is True


async def test_assist_selector_miss_carries_live_session_cleanup(
    store: FakeStore, bus: FakeBus, monkeypatch: pytest.MonkeyPatch
):
    reservation = stage(store, bus, ReservationMode.ASSIST)
    closed = False

    async def cleanup() -> None:
        nonlocal closed
        closed = True

    async def open_page(_url: str):
        return FakePage(known={}), cleanup

    monkeypatch.setattr(reservation, "_open", open_page)

    with pytest.raises(SelectorMiss) as excinfo:
        await reservation.reserve(message())

    assert excinfo.value.page is not None
    assert excinfo.value.cleanup is cleanup
    assert closed is False

    await excinfo.value.cleanup()
    assert closed is True


# ------------------------------------------------------------------- alert


async def test_alert_mode_returns_the_deep_link_without_a_browser(store: FakeStore, bus: FakeBus):
    result = await stage(store, bus, ReservationMode.ALERT).reserve(message())

    assert result.ok is True
    assert result.reservation_url == "https://vendor.example/checkout/1234"
    assert result.forced_alert is False


async def test_alert_mode_falls_back_to_the_event_url(store: FakeStore, bus: FakeBus):
    result = await stage(store, bus, ReservationMode.ALERT).reserve(message(ticket_link=""))

    assert result.reservation_url == "https://ra.co/events/1234"


# --------------------------------------------------------------- selectors


def test_domain_extraction_strips_www():
    assert domain_of("https://www.vendor.example/a/b?c=d") == "vendor.example"
    assert domain_of("not a url") == ""


async def test_resolver_prefers_the_redis_override(store: FakeStore, bus: FakeBus):
    bus.cache[("vendor.example", "buy_button")] = "#cached"
    store.selectors[("vendor.example", "buy_button")] = "#durable"

    candidates = await SelectorResolver(cache=bus, store=store).resolve(
        "vendor.example", "buy_button"
    )

    assert candidates[0] == "#cached"


async def test_resolver_warms_the_cache_from_postgres(store: FakeStore, bus: FakeBus):
    store.selectors[("vendor.example", "buy_button")] = "#durable"

    candidates = await SelectorResolver(cache=bus, store=store).resolve(
        "vendor.example", "buy_button"
    )

    assert candidates[0] == "#durable"
    assert bus.cache[("vendor.example", "buy_button")] == "#durable"


async def test_resolver_falls_back_to_built_in_defaults(store: FakeStore, bus: FakeBus):
    candidates = await SelectorResolver(cache=bus, store=store).resolve(
        "vendor.example", "buy_button"
    )

    assert "[data-testid='buy-button']" in candidates


async def test_locate_raises_selector_miss_carrying_the_live_page(store: FakeStore, bus: FakeBus):
    page = FakePage(known={})

    with pytest.raises(SelectorMiss) as excinfo:
        await stage(store, bus, ReservationMode.ASSIST)._locate(
            page, "vendor.example", "buy_button", "https://vendor.example/x"
        )

    assert excinfo.value.page is page
    assert excinfo.value.domain == "vendor.example"
    assert len(excinfo.value.tried) > 0


async def test_locate_returns_the_first_usable_candidate(store: FakeStore, bus: FakeBus):
    page = FakePage(
        known={
            "[data-testid='buy-button']": FakeLocator(matches=2),
            "button[data-testid*='buy' i]": FakeLocator(matches=1),
        }
    )

    selector = await stage(store, bus, ReservationMode.ASSIST)._locate(
        page, "vendor.example", "buy_button", "https://vendor.example/x"
    )

    assert selector == "button[data-testid*='buy' i]"
