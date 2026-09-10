from __future__ import annotations

from conftest import FakeBus, FakeLocator, FakePage, FakeStore
from harness.browser.page import validate_selector
from harness.healing.llm import (
    ClaudeClient,
    FakeLLMClient,
    LLMError,
    SelectorCandidate,
    build,
    parse_candidates,
)
from harness.healing.repair import SelfHealer

DOMAIN = "vendor.example"
URL = "https://vendor.example/checkout/1234"


def healer(store: FakeStore, bus: FakeBus, llm=None) -> SelfHealer:
    return SelfHealer(llm or FakeLLMClient(), bus, store)


# ------------------------------------------------------------- validation


async def test_validation_requires_exactly_one_match():
    page = FakePage(known={"#go": FakeLocator(matches=3)})

    assert await validate_selector(page, "#go") is False


async def test_validation_requires_visible_and_enabled():
    page = FakePage(
        known={
            "#hidden": FakeLocator(matches=1, visible=False),
            "#disabled": FakeLocator(matches=1, enabled=False),
            "#good": FakeLocator(matches=1),
        }
    )

    assert await validate_selector(page, "#hidden") is False
    assert await validate_selector(page, "#disabled") is False
    assert await validate_selector(page, "#good") is True


async def test_invalid_css_fails_validation_rather_than_raising():
    class ExplodingPage(FakePage):
        def locator(self, selector: str):
            raise ValueError("unsupported pseudo-class")

    assert await validate_selector(ExplodingPage(), ":has-text(") is False


# ------------------------------------------------------------ candidate parsing


def test_parse_candidates_tolerates_a_code_fence_and_preamble():
    text = 'Here you go:\n```json\n[{"selector": "#buy", "confidence": 0.9}]\n```'

    [candidate] = parse_candidates(text)

    assert candidate.selector == "#buy"
    assert candidate.confidence == 0.9


def test_parse_candidates_returns_nothing_for_prose():
    assert parse_candidates("I could not find a buy button.") == []


def test_parse_candidates_skips_entries_without_a_selector():
    assert parse_candidates('[{"confidence": 0.9}, {"selector": "#a"}]') == [
        SelectorCandidate("#a", 0.0, "")
    ]


def test_build_uses_the_fake_client_without_a_key():
    assert build(api_key="", model="claude-3-5-sonnet-latest", timeout_ms=1000).name == "fake"


def test_build_constructs_the_real_client_with_a_key():
    """Constructs but never calls. Enough to catch an SDK import or signature
    break at test time rather than on the first real selector miss."""
    client = build(
        api_key="sk-ant-not-a-real-key", model="claude-3-5-sonnet-latest", timeout_ms=1000
    )

    assert client.name == "claude"
    assert isinstance(client, ClaudeClient)


# -------------------------------------------------------------------- heal


async def test_successful_heal_writes_through_to_both_stores(store: FakeStore, bus: FakeBus):
    page = FakePage(known={"[data-testid='buy-button']": FakeLocator(matches=1)})

    result = await healer(store, bus).heal(
        page=page, domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.ok is True
    assert result.selector == "[data-testid='buy-button']"
    assert bus.cache[(DOMAIN, "buy_button")] == "[data-testid='buy-button']"
    assert store.saved_selectors[0]["domain"] == DOMAIN
    assert store.saved_selectors[0]["healed_from"] == URL


async def test_unvalidated_candidates_are_never_adopted(store: FakeStore, bus: FakeBus):
    """The model's top pick matches four elements, so it must not be persisted."""
    llm = FakeLLMClient([SelectorCandidate("button", 0.99, "everything is a button")])
    page = FakePage(known={"button": FakeLocator(matches=4)})

    result = await healer(store, bus, llm).heal(
        page=page, domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.ok is False
    assert "validation" in result.reason
    assert store.saved_selectors == []
    assert bus.cache == {}


async def test_candidates_are_tried_in_confidence_order(store: FakeStore, bus: FakeBus):
    llm = FakeLLMClient(
        [
            SelectorCandidate("#weak", 0.2, ""),
            SelectorCandidate("#strong", 0.95, ""),
        ]
    )
    page = FakePage(known={"#weak": FakeLocator(matches=1), "#strong": FakeLocator(matches=1)})

    result = await healer(store, bus, llm).heal(
        page=page, domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.selector == "#strong"


async def test_empty_model_response_fails_the_heal(store: FakeStore, bus: FakeBus):
    result = await healer(store, bus, FakeLLMClient([])).heal(
        page=FakePage(), domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.ok is False
    assert result.reason == "model returned no candidates"


async def test_llm_error_fails_the_heal_without_propagating(store: FakeStore, bus: FakeBus):
    class BrokenLLM:
        name = "broken"

        async def suggest_selectors(self, **kwargs):
            raise LLMError("overloaded")

        async def aclose(self):
            return None

    result = await healer(store, bus, BrokenLLM()).heal(
        page=FakePage(), domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.ok is False
    assert "overloaded" in result.reason


async def test_dead_page_fails_the_heal(store: FakeStore, bus: FakeBus):
    page = FakePage(content_error=RuntimeError("target closed"))

    result = await healer(store, bus).heal(
        page=page, domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.ok is False
    assert "target closed" in result.reason


async def test_heal_reports_the_dom_reduction_it_achieved(store: FakeStore, bus: FakeBus):
    page = FakePage(
        html="<html><script>" + "x" * 50_000 + "</script><button id='b'>Buy</button></html>",
        known={"[data-testid='buy-button']": FakeLocator(matches=1)},
    )

    result = await healer(store, bus).heal(
        page=page, domain=DOMAIN, logical_name="buy_button", url=URL
    )

    assert result.original_bytes > 50_000
    assert result.cleaned_bytes < 1_000
