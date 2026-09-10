"""Logical name to CSS selector resolution, with a two-level override cache.

Vendors are not special-cased by name. A logical name like `buy_button` has a
generic fallback chain, and anything site-specific arrives as an override that
the self-heal stage learned and wrote through. That keeps a new vendor from
becoming a code change.
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse

BUY_BUTTON = "buy_button"

# Ordered fallbacks, most specific first. Role and data-testid before text,
# because text matching breaks on the first localized page.
DEFAULT_SELECTORS: dict[str, tuple[str, ...]] = {
    BUY_BUTTON: (
        "[data-testid='buy-button']",
        "button[data-testid*='buy' i]",
        "a[data-testid*='buy' i]",
        "[role='button'][aria-label*='buy' i]",
        "[role='button'][aria-label*='ticket' i]",
        "button#buy",
        "a.buy-tickets",
    ),
}


def domain_of(url: str) -> str:
    """Registrable-ish host, lowercased, `www.` stripped.

    Overrides are keyed per domain rather than per URL because a healed
    checkout selector is a property of the vendor's template, not of one
    event's page.
    """
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class SelectorCache(Protocol):
    async def cached_selector(self, domain: str, logical_name: str) -> str | None: ...

    async def cache_selector(self, domain: str, logical_name: str, selector: str) -> None: ...


class SelectorStore(Protocol):
    async def selector_override(self, domain: str, logical_name: str) -> str | None: ...


class SelectorResolver:
    """Redis first, Postgres second, built-in defaults last.

    Redis is the hot cache and Postgres is the durable mirror, so a Redis flush
    costs one extra query rather than re-running a Claude call per drop.
    """

    def __init__(self, cache: SelectorCache, store: SelectorStore) -> None:
        self._cache = cache
        self._store = store

    async def resolve(self, domain: str, logical_name: str) -> list[str]:
        candidates: list[str] = []

        cached = await self._cache.cached_selector(domain, logical_name)
        if cached:
            candidates.append(cached)
        else:
            stored = await self._store.selector_override(domain, logical_name)
            if stored:
                candidates.append(stored)
                await self._cache.cache_selector(domain, logical_name, stored)

        candidates.extend(DEFAULT_SELECTORS.get(logical_name, ()))
        return candidates
