"""The narrow slice of Playwright the harness actually uses.

Everything downstream of RESERVING is typed against these protocols rather
than against Playwright itself, so selector validation and self-healing can be
unit tested without a browser binary on disk. `playwright install` downloads
hundreds of megabytes; making that a prerequisite for running the test suite
would mean the suite stops being run.
"""

from __future__ import annotations

from typing import Protocol


class LocatorLike(Protocol):
    async def count(self) -> int: ...

    async def is_visible(self, *, timeout: float | None = None) -> bool: ...

    async def is_enabled(self, *, timeout: float | None = None) -> bool: ...

    async def text_content(self, *, timeout: float | None = None) -> str | None: ...

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None: ...


class PageLike(Protocol):
    url: str

    def locator(self, selector: str) -> LocatorLike: ...

    async def content(self) -> str: ...


async def validate_selector(page: PageLike, selector: str) -> bool:
    """Exactly one match, visible, and enabled.

    This is the gate that stops ARCHITECTURE.md §9 failure 6: an LLM will
    happily return a plausible selector that matches the wrong control, or
    fourteen controls. Requiring a unique match is the cheapest property that
    makes "clicked the wrong button on a real checkout" impossible rather than
    unlikely. An invalid CSS expression is a failed validation, not an
    exception — a bad suggestion should cost one branch, not the drop.
    """
    try:
        locator = page.locator(selector)
        if await locator.count() != 1:
            return False
        return bool(await locator.is_visible()) and bool(await locator.is_enabled())
    except Exception:  # noqa: BLE001 - any locator fault means "did not validate"
        return False
