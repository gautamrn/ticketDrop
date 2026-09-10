from __future__ import annotations

import httpx
import pytest

from harness.embeddings import (
    DeterministicEmbedder,
    EmbeddingError,
    VoyageEmbedder,
    build,
    to_pgvector,
)


async def test_deterministic_embedder_is_stable_across_instances():
    first = await DeterministicEmbedder().embed(["DVS1"])
    second = await DeterministicEmbedder().embed(["DVS1"])

    assert first == second


async def test_deterministic_embedder_normalizes_to_the_schema_dimension():
    [vector] = await DeterministicEmbedder(dimension=1024).embed(["Spot Lite"])

    assert len(vector) == 1024
    assert sum(x * x for x in vector) == pytest.approx(1.0, abs=1e-9)


async def test_deterministic_embedder_ignores_case_and_padding():
    [a] = await DeterministicEmbedder().embed(["  Rrose "])
    [b] = await DeterministicEmbedder().embed(["rrose"])

    assert a == b


async def test_deterministic_embedder_separates_distinct_texts():
    a, b = await DeterministicEmbedder().embed(["DVS1", "Rrose"])

    assert a != b


async def test_voyage_embedder_orders_by_returned_index():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0] * 4},
                    {"index": 0, "embedding": [1.0] * 4},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        embedder = VoyageEmbedder("key", dimension=4, client=client)
        vectors = await embedder.embed(["first", "second"])

    assert vectors[0] == [1.0] * 4
    assert vectors[1] == [0.0] * 4


async def test_voyage_embedder_rejects_a_dimension_the_schema_cannot_store():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1536}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = VoyageEmbedder("key", dimension=1024, client=client)
        with pytest.raises(EmbeddingError, match="dimension"):
            await embedder.embed(["x"])


async def test_voyage_embedder_surfaces_http_failures_as_embedding_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = VoyageEmbedder("key", dimension=1024, client=client)
        with pytest.raises(EmbeddingError, match="429"):
            await embedder.embed(["x"])


def test_build_selects_deterministic_without_a_key():
    embedder = build(
        api_key="", model="voyage-3", dimension=1024, base_url="https://x", timeout_ms=1000
    )

    assert embedder.name == "deterministic"


def test_build_selects_voyage_with_a_key():
    embedder = build(
        api_key="k", model="voyage-3", dimension=1024, base_url="https://x", timeout_ms=1000
    )

    assert embedder.name == "voyage"


def test_pgvector_literal_format():
    assert to_pgvector([1.0, -0.5, 0.25]) == "[1,-0.5,0.25]"
