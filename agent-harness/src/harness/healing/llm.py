"""Selector inference behind a protocol.

Same shape as the embedder: a real client when a key exists, a canned one when
it does not, chosen by config. The fake is not only a test double — it is what
runs in development, so the self-heal path is exercised end to end without an
Anthropic account.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from ..log import get

_log = get("harness.healing.llm")

_JSON_BLOCK = re.compile(r"\[.*\]", re.DOTALL)

_SYSTEM_PROMPT = """You repair broken CSS selectors for a browser automation \
stage. You are given a stripped HTML document and the logical name of a control \
that could not be found.

Return ONLY a JSON array, no prose and no code fences. Each element:
{"selector": "<css>", "confidence": <0..1>, "rationale": "<short>"}

Rules:
- Return at most 3 candidates, best first.
- Each selector must match exactly one element in the document provided.
- Prefer stable hooks: data-testid, id, role, aria-label. Avoid selectors that \
depend on generated class hashes or on nth-child position.
- If nothing in the document plausibly is that control, return []."""


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SelectorCandidate:
    selector: str
    confidence: float
    rationale: str = ""


class LLMClient(Protocol):
    name: str

    async def suggest_selectors(
        self, *, cleaned_html: str, logical_name: str, domain: str, url: str
    ) -> list[SelectorCandidate]: ...

    async def aclose(self) -> None: ...


class FakeLLMClient:
    """Canned candidates. Used in tests and whenever ANTHROPIC_API_KEY is unset.

    Returns the generic hooks a real model would propose first, so the
    validation, write-through, and retry-cap logic all run for real; only the
    inference is stubbed. Empty by default is wrong here — a fake that never
    heals would make the FAILED path the only tested one.
    """

    name = "fake"

    def __init__(self, candidates: list[SelectorCandidate] | None = None) -> None:
        self._candidates = candidates if candidates is not None else _DEFAULT_FAKE_CANDIDATES

    async def suggest_selectors(
        self, *, cleaned_html: str, logical_name: str, domain: str, url: str
    ) -> list[SelectorCandidate]:
        _log.warning(
            "using fake LLM client; healed selectors are guesses, not inference",
            logical_name=logical_name,
            domain=domain,
        )
        return list(self._candidates)

    async def aclose(self) -> None:
        return None


_DEFAULT_FAKE_CANDIDATES = [
    SelectorCandidate("[data-testid='buy-button']", 0.5, "canned: conventional test hook"),
    SelectorCandidate("button[aria-label*='ticket' i]", 0.4, "canned: accessible name"),
]


class ClaudeClient:
    """Anthropic Messages API, temperature 0, structured output by convention.

    Temperature 0 because this is a lookup over a document that was just
    handed over, not a creative task; a sampled answer would make an identical
    failure heal differently on two workers. The response is still parsed
    defensively — a model that returns prose is a bad heal, not a crash.
    """

    name = "claude"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-3-5-sonnet-latest",
        timeout_s: float = 15.0,
        max_tokens: int = 1024,
    ) -> None:
        if not api_key:
            raise ValueError("ClaudeClient requires an API key")
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s)
        self._model = model
        self._max_tokens = max_tokens

    async def suggest_selectors(
        self, *, cleaned_html: str, logical_name: str, domain: str, url: str
    ) -> list[SelectorCandidate]:
        prompt = (
            f"Site: {domain}\nPage: {url}\nMissing control: {logical_name}\n\n"
            f"Document:\n{cleaned_html}"
        )
        try:
            message = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            # The SDK raises a wide family (connection, overload, auth); all of
            # them mean the same thing to the caller: no candidates this time.
            raise LLMError(f"anthropic request failed: {exc}") from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        return parse_candidates(text)

    async def aclose(self) -> None:
        await self._client.close()


def parse_candidates(text: str) -> list[SelectorCandidate]:
    """Pull the JSON array out of a model response.

    Tolerant of a code fence or a sentence of preamble, intolerant of anything
    that is not a list of objects with a selector. A malformed response yields
    no candidates, which routes the drop to FAILED rather than executing
    something unparsed.
    """
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    candidates: list[SelectorCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        selector = str(item.get("selector", "")).strip()
        if not selector:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        candidates.append(
            SelectorCandidate(
                selector=selector,
                confidence=max(0.0, min(1.0, confidence)),
                rationale=str(item.get("rationale", ""))[:280],
            )
        )
    return candidates


def build(*, api_key: str, model: str, timeout_ms: int) -> LLMClient:
    if api_key:
        return ClaudeClient(api_key=api_key, model=model, timeout_s=timeout_ms / 1000.0)
    return FakeLLMClient()
