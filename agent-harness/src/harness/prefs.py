"""Preference management.

This exists because hype scoring fails closed. With no rows in
`user_preferences`, artist, sound, and venue affinity all report "no signal" and
contribute nothing, capping every drop at the 0.15 demand weight — below both the
0.80 reservation threshold and the 0.65 promotion threshold. The system then runs
perfectly, consumes every message, and does nothing, forever, without an error.

Failing closed is the right policy for a stage that drives a browser. Making that
state discoverable is this module's job.

Note that seeded preferences are useful even without a Voyage key: the
deterministic fallback embedder maps identical text to an identical vector, so an
exact name match still scores 1.0. What it cannot do is generalize — "Prospa" will
not match "Prospa (Live)" — so keyless runs exercise the plumbing, not the
judgment.
"""

from __future__ import annotations

from dataclasses import dataclass

from .embeddings import Embedder
from .store import PgStore

# Mirrors the preference_kind enum. 'sound' is excluded: it is scored against a
# 12-dimensional audio signature rather than a text embedding, so it cannot be
# seeded from a label.
TEXT_KINDS = ("artist", "venue", "genre", "freeform")


@dataclass(frozen=True, slots=True)
class Preference:
    label: str
    kind: str
    weight: float
    active: bool


async def add(
    store: PgStore, embedder: Embedder, *, label: str, kind: str, weight: float = 1.0
) -> None:
    if kind not in TEXT_KINDS:
        raise ValueError(f"kind must be one of {', '.join(TEXT_KINDS)}, got {kind!r}")
    label = label.strip()
    if not label:
        raise ValueError("label must not be empty")

    vectors = await embedder.embed([label])
    if not vectors:
        raise ValueError(f"embedder returned no vector for {label!r}")
    await store.add_preference(label=label, kind=kind, vector=vectors[0], weight=weight)


async def listing(store: PgStore) -> list[Preference]:
    rows = await store.list_preferences()
    return [
        Preference(label=r["label"], kind=r["kind"], weight=r["weight"], active=r["active"])
        for r in rows
    ]
