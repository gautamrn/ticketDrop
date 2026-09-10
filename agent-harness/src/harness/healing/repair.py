"""The self-heal attempt. One per drop, no exceptions.

ARCHITECTURE.md §7. The retry cap is the whole safety property: each heal costs
a Sonnet call and ~1 s, so an uncapped loop against a site-wide redesign would
occupy every worker at once and turn a cosmetic breakage into a total outage.
The cap is enforced twice over — the FSM refuses to enter SELF_HEAL when
attempt_count is already at the limit, and the graph has no edge back into
RESERVING, so there is no cycle to run away with even if the counter were wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..browser.page import PageLike, validate_selector
from ..browser.selectors import SelectorCache
from ..log import get
from .dom import clean_html
from .llm import LLMClient, LLMError, SelectorCandidate

_log = get("harness.healing")


class SelectorPersistence(Protocol):
    async def save_selector_override(
        self,
        *,
        domain: str,
        logical_name: str,
        selector: str,
        confidence: float | None,
        model: str,
        healed_from: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class HealResult:
    ok: bool
    selector: str = ""
    confidence: float | None = None
    reason: str = ""
    candidates_considered: int = 0
    cleaned_bytes: int = 0
    original_bytes: int = 0


class SelfHealer:
    def __init__(
        self,
        llm: LLMClient,
        cache: SelectorCache,
        store: SelectorPersistence,
        *,
        max_html_bytes: int = 120_000,
        max_candidates: int = 3,
    ) -> None:
        self._llm = llm
        self._cache = cache
        self._store = store
        self._max_html_bytes = max_html_bytes
        self._max_candidates = max_candidates

    async def heal(self, *, page: PageLike, domain: str, logical_name: str, url: str) -> HealResult:
        try:
            html = await page.content()
        except Exception as exc:  # noqa: BLE001 - a dead page cannot be healed
            return HealResult(ok=False, reason=f"page content unavailable: {exc}")

        cleaned = clean_html(html, max_bytes=self._max_html_bytes)
        _log.info(
            "dom cleaned for heal",
            domain=domain,
            logical_name=logical_name,
            original_bytes=len(html),
            cleaned_bytes=len(cleaned),
        )

        try:
            candidates = await self._llm.suggest_selectors(
                cleaned_html=cleaned, logical_name=logical_name, domain=domain, url=url
            )
        except LLMError as exc:
            return HealResult(
                ok=False,
                reason=str(exc),
                cleaned_bytes=len(cleaned),
                original_bytes=len(html),
            )

        ranked = sorted(candidates, key=lambda c: c.confidence, reverse=True)[
            : self._max_candidates
        ]
        if not ranked:
            return HealResult(
                ok=False,
                reason="model returned no candidates",
                cleaned_bytes=len(cleaned),
                original_bytes=len(html),
            )

        for candidate in ranked:
            if not await validate_selector(page, candidate.selector):
                _log.info(
                    "candidate rejected by in-page validation",
                    domain=domain,
                    logical_name=logical_name,
                    selector=candidate.selector,
                )
                continue
            await self._persist(domain, logical_name, candidate, url)
            return HealResult(
                ok=True,
                selector=candidate.selector,
                confidence=candidate.confidence,
                candidates_considered=len(ranked),
                cleaned_bytes=len(cleaned),
                original_bytes=len(html),
            )

        return HealResult(
            ok=False,
            reason="no candidate passed in-page validation",
            candidates_considered=len(ranked),
            cleaned_bytes=len(cleaned),
            original_bytes=len(html),
        )

    async def _persist(
        self, domain: str, logical_name: str, candidate: SelectorCandidate, url: str
    ) -> None:
        """Write through to both stores, Postgres first.

        Postgres is the durable record and Redis is the cache, so the cache is
        populated second: a crash between the two leaves a warm read that costs
        one query, whereas the reverse order would leave a cached selector with
        no provenance row to audit.
        """
        await self._store.save_selector_override(
            domain=domain,
            logical_name=logical_name,
            selector=candidate.selector,
            confidence=candidate.confidence,
            model=self._llm.name,
            healed_from=url,
        )
        await self._cache.cache_selector(domain, logical_name, candidate.selector)
        _log.info(
            "selector healed",
            domain=domain,
            logical_name=logical_name,
            selector=candidate.selector,
            confidence=candidate.confidence,
            model=self._llm.name,
        )
