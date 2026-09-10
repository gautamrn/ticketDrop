"""Text embedding behind a protocol, so hype scoring runs without an API key.

Anthropic exposes no embeddings endpoint, so the schema is dimensioned for
Voyage AI voyage-3 at 1024-d (ARCHITECTURE.md §6). Both implementations here
must emit exactly that dimension: pgvector fixes it at column creation, so a
mismatch is a failed INSERT rather than a worse score.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx
import numpy as np

from .log import get

_log = get("harness.embeddings")


class EmbeddingError(RuntimeError):
    """Embedding could not be produced. Treated as a missing signal, not a crash."""


@runtime_checkable
class Embedder(Protocol):
    name: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one unit-norm vector per input, in input order."""
        ...

    async def aclose(self) -> None: ...


class DeterministicEmbedder:
    """Keyless fallback. Stable, normalized, and semantically meaningless.

    This exists so the pipeline is runnable and testable with no credentials.
    The vectors carry no semantics: two texts about the same genre are as far
    apart as two unrelated ones. Any hype score computed from these is a
    plumbing check, not a judgment, which is why it warns on construction
    rather than on some later line nobody reads.

    Determinism comes from seeding a PCG64 stream with a BLAKE2b digest of the
    text, so the same string yields the same vector across processes and runs
    (Python's hash() is salted per process and would not).
    """

    name = "deterministic"

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        _log.warning(
            "using deterministic embedder; hype scores are not semantically meaningful",
            dimension=dimension,
            reason="VOYAGE_API_KEY is not set",
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.blake2b(text.strip().lower().encode("utf-8"), digest_size=8).digest()
        rng = np.random.default_rng(int.from_bytes(digest, "big"))
        vec = rng.standard_normal(self.dimension)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            raise EmbeddingError("degenerate zero vector")
        return (vec / norm).astype(float).tolist()

    async def aclose(self) -> None:
        return None


class VoyageEmbedder:
    """voyage-3 over HTTP.

    One client, reused: the evaluate stage has a 350 ms budget and a fresh TLS
    handshake per drop would spend most of it. Errors raise EmbeddingError so
    the caller degrades the affected signal instead of failing the drop; an
    embedding outage should cost recall, not the whole FSM.
    """

    name = "voyage"

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3",
        dimension: int = 1024,
        base_url: str = "https://api.voyageai.com/v1",
        timeout_s: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("VoyageEmbedder requires an API key")
        self.dimension = dimension
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": list(texts), "input_type": "document"},
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            raise EmbeddingError(f"voyage request failed: {exc}") from exc

        if response.status_code != 200:
            raise EmbeddingError(f"voyage returned {response.status_code}: {response.text[:200]}")

        payload = response.json()
        # Voyage documents the response as ordered by input index, but the
        # index field is authoritative; sorting on it means a future batching
        # change upstream cannot silently misattribute a vector to an artist.
        items = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        if len(items) != len(texts):
            raise EmbeddingError(f"voyage returned {len(items)} vectors for {len(texts)} inputs")

        vectors: list[list[float]] = []
        for item in items:
            vec = item.get("embedding") or []
            if len(vec) != self.dimension:
                raise EmbeddingError(
                    f"voyage returned dimension {len(vec)}, schema requires {self.dimension}"
                )
            vectors.append([float(x) for x in vec])
        return vectors

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build(
    *,
    api_key: str,
    model: str,
    dimension: int,
    base_url: str,
    timeout_ms: int,
) -> Embedder:
    """Pick an implementation from config. A missing key degrades, never fails."""
    if api_key:
        return VoyageEmbedder(
            api_key=api_key,
            model=model,
            dimension=dimension,
            base_url=base_url,
            timeout_s=timeout_ms / 1000.0,
        )
    return DeterministicEmbedder(dimension=dimension)


def to_pgvector(vector: Sequence[float]) -> str:
    """pgvector's text input format. asyncpg has no native vector codec."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"
