"""The bounded reservation stage.

ARCHITECTURE.md §10. The mode ladder is a legal boundary, not a feature flag:
the US BOTS Act and the UK Breaching of Limits on Ticket Sales Regulations
prohibit circumventing waiting rooms, CAPTCHAs, and per-person purchase limits.
`alert` observes and notifies, while `assist` drives a headed browser up to the
point a human takes over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import ReservationMode
from ..log import get
from ..messages import DropMessage
from .page import PageLike, validate_selector
from .selectors import BUY_BUTTON, SelectorResolver, domain_of

_log = get("harness.reserve")


class ReservationError(RuntimeError):
    """A failure that is not a selector problem: navigation, 5xx, auth wall.

    Kept distinct from SelectorMiss because ARCHITECTURE.md §7 routes these
    straight to FAILED. Asking an LLM to find a buy button on a 503 page burns
    the self-heal budget on a page that has no buy button to find.
    """


class SelectorMiss(Exception):
    """A locator timeout on a page that loaded fine. The only self-heal trigger.

    Carries the live page rather than a copy of the HTML: the repair has to be
    validated against the same DOM that failed, and a re-fetch can render
    differently under A/B tests or a sold-out state change.
    """

    def __init__(
        self,
        *,
        logical_name: str,
        url: str,
        domain: str,
        tried: tuple[str, ...],
        page: PageLike | None = None,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(f"selector miss for {logical_name!r} on {domain}")
        self.logical_name = logical_name
        self.url = url
        self.domain = domain
        self.tried = tried
        self.page = page
        self.cleanup = cleanup


@dataclass(slots=True)
class ReservationResult:
    ok: bool
    mode: ReservationMode
    reservation_url: str
    # True when queue_it overrode the configured mode. Surfaced in the results
    # stream so the admin panel can explain why a hot drop was alert-only.
    forced_alert: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def resolve_mode(configured: ReservationMode, *, queue_it: bool) -> tuple[ReservationMode, bool]:
    """Pick the effective mode. queue_it wins over everything.

    An event behind a Queue-it waiting room is always alert-only because the
    waiting-room constraint carries legal weight.
    """
    if queue_it:
        return ReservationMode.ALERT, configured is not ReservationMode.ALERT
    return configured, False


class ReservationStage:
    def __init__(
        self,
        resolver: SelectorResolver,
        *,
        mode: ReservationMode,
        locator_timeout_ms: int = 5_000,
        navigation_timeout_ms: int = 15_000,
    ) -> None:
        self._resolver = resolver
        self._mode = mode
        self._locator_timeout_ms = locator_timeout_ms
        self._navigation_timeout_ms = navigation_timeout_ms

    async def reserve(self, msg: DropMessage) -> ReservationResult:
        mode, forced = resolve_mode(self._mode, queue_it=msg.queue_it)
        if forced:
            _log.info(
                "queue_it event forced to alert-only",
                drop_id=msg.drop_id,
                configured_mode=self._mode.value,
            )

        if mode is ReservationMode.ALERT:
            return self._alert(msg, forced_alert=forced)
        return await self._assist(msg)

    def _alert(self, msg: DropMessage, *, forced_alert: bool) -> ReservationResult:
        """No browser, no page load, no Playwright import.

        This is the default and the only path with zero legal exposure, so it
        is also the only path that must work on a machine where
        `playwright install` was never run.
        """
        return ReservationResult(
            ok=True,
            mode=ReservationMode.ALERT,
            reservation_url=msg.deep_link,
            forced_alert=forced_alert,
            detail={"notified_via": "deep_link", "queue_it": msg.queue_it},
        )

    async def _assist(self, msg: DropMessage) -> ReservationResult:
        """Headed browser, navigate, locate the CTA, hand the session over.

        The browser is headed and the session is left open deliberately: the
        human completes CAPTCHA, queue, and payment. Nothing here clicks a
        control that would advance a transaction.
        """
        url = msg.deep_link
        if not url:
            raise ReservationError("no ticket_link or event_url to navigate to")

        page, cleanup = await self._open(url)
        domain = domain_of(url)
        try:
            selector = await self._locate(page, domain, BUY_BUTTON, url)
        except SelectorMiss as miss:
            miss.cleanup = cleanup
            raise
        except Exception:
            await cleanup()
            raise
        # The Playwright session deliberately remains open: on success the
        # human takes over, and on SelectorMiss the FSM passes this same live
        # page to the self-heal node.
        return ReservationResult(
            ok=True,
            mode=ReservationMode.ASSIST,
            reservation_url=url,
            detail={"selector": selector, "handoff": "human", "domain": domain},
        )

    async def _open(
        self, url: str
    ) -> tuple[PageLike, Callable[[], Awaitable[None]]]:  # pragma: no cover - needs a browser
        """Import Playwright lazily so `alert` never pays for it.

        Importing at module scope would make a missing browser install an
        import-time concern for a mode that does not use a browser.
        """
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        try:
            browser = await manager.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto(url, timeout=self._navigation_timeout_ms)
        except Exception as exc:
            await manager.stop()
            raise ReservationError(f"navigation failed for {url}: {exc}") from exc

        if response is not None and response.status >= 400:
            status = response.status
            await manager.stop()
            # Not a selector problem. A 403 is an auth wall and a 503 is the
            # vendor melting under the same drop we are reacting to; neither
            # gets better because an LLM looked at the HTML.
            raise ReservationError(f"page returned HTTP {status} for {url}")

        async def cleanup() -> None:
            await manager.stop()

        return page, cleanup

    async def _locate(self, page: PageLike, domain: str, logical_name: str, url: str) -> str:
        """First candidate that resolves to exactly one usable element.

        A miss raises SelectorMiss carrying the live page, because the self-heal
        stage needs the same DOM that failed, not a re-fetch that may have
        rendered differently.
        """
        candidates = await self._resolver.resolve(domain, logical_name)
        for selector in candidates:
            if await validate_selector(page, selector):
                return selector
        raise SelectorMiss(
            logical_name=logical_name,
            url=url,
            domain=domain,
            tried=tuple(candidates),
            page=page,
        )
