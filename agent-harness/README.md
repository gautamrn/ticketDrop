# agent-harness

Python service that consumes `ticket:drops`, scores each drop, and runs the
bounded reservation stage. It is the only reader of the `tdrop-harness`
consumer group and the only writer to `ticket:drops:results`, `drops`, and
`drop_transitions`.

It never polls a ticketing source. Everything it knows arrives as a Redis
Stream message published by the Go ingester.

## Layout

```
src/harness/config.py       environment loading and validation
src/harness/messages.py     the ticket:drops message contract
src/harness/bus.py          consumer group, idempotency claim, results stream
src/harness/store.py        all Postgres access, behind a Store protocol
src/harness/embeddings.py   Embedder protocol: Voyage, or a keyless fallback
src/harness/scoring.py      the weighted hype blend
src/harness/fsm/state.py    graph state and route labels
src/harness/fsm/nodes.py    one node per drop_state
src/harness/fsm/graph.py    the compiled StateGraph and its deadlines
src/harness/browser/        selector resolution and the reservation modes
src/harness/healing/        DOM reduction, LLM client, the one-shot repair
src/harness/worker.py       intake, dedupe, dead-lettering, concurrency ceiling
src/harness/app.py          composition root
scripts/e2e_smoke.py        live Postgres + Redis smoke test
scripts/check_autoclaim.py  live XAUTOCLAIM recovery check
```

## Running with no API keys

This is the default and it is a design constraint, not a limitation to work
around. Both external services are behind a protocol with a keyless
implementation chosen by config:

| Missing key | Fallback | Consequence |
| --- | --- | --- |
| `VOYAGE_API_KEY` | `DeterministicEmbedder` | Stable, normalized 1024-d vectors seeded from a hash of the text. **Scores are not semantically meaningful.** Logs a warning on construction. |
| `ANTHROPIC_API_KEY` | `FakeLLMClient` | Returns canned conventional selectors. Validation, write-through, and the retry cap all run for real; only the inference is stubbed. |

The default `RESERVATION_MODE=alert` launches no browser at all, so nothing
here requires `playwright install`.

```powershell
cd agent-harness
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m harness
```

## The FSM

```
INGESTED -> EVALUATING -+-> SKIPPED
                        |
                        +-> NOTIFIED            (announced, score >= 0.65)
                        |
                        +-> RESERVING -+-> NOTIFIED
                                       +-> SELF_HEAL -+-> NOTIFIED
                                       |              +-> FAILED
                                       +-> FAILED
```

The graph is **acyclic**. ARCHITECTURE.md §4 permits SELF_HEAL to resume the
reservation once, but modelling that as an edge back into RESERVING would make
the one-attempt cap depend entirely on a counter being right. Instead SELF_HEAL
validates the repaired selector against the live page and terminates, so the
retry budget is a property of the graph's shape. A topology that cannot loop
cannot starve the worker pool.

Discovery-tier messages (`change == "announced"`) branch to watchlist promotion
rather than to reservation. At announcement time there is nothing to reserve —
EDMTrain does not know whether tickets are on sale.

Every node records its entry into `drop_transitions` **before** doing its work,
so `latency_ms` on a row is the time spent in the *previous* state. A row reads
"we left EVALUATING for RESERVING after 210 ms", which is the per-stage number
the latency budget in ARCHITECTURE.md §3 is written against.

## Hype scoring

Four signals, weights from ARCHITECTURE.md §6: artist 0.40, sound 0.25, venue
0.20, demand 0.15. Similarity is pgvector cosine distance against
`user_preferences`.

A missing signal contributes **zero to the numerator while the denominator
stays at 1.0**. The weights are never renormalized over the signals that
happened to survive, because renormalizing would let a drop with three dead
signals and one strong artist match score 1.0 and walk into RESERVING. The
consequence is that an empty `user_preferences` table scores everything at 0.0
and reserves nothing, which is the right default for a stage that drives a
browser.

Demand velocity is a rate, not a count: attendees per hour since `first_seen_at`,
floored at one hour and saturated rather than scaled. 400 attending on a show
announced six months ago is a normal sell-through; 400 in an afternoon is the
drop.

## Reservation modes

| Mode | Behavior |
| --- | --- |
| `alert` *(default)* | No browser. Notify with the deep link, unmodified. |
| `assist` | Headed browser, navigate, locate the CTA, hand the live session to the human. Never completes a transaction. |

`queue_it == true` forces `alert` regardless of the configured mode. A Queue-it
waiting room is the constraint with legal weight behind it (US BOTS Act, UK
Breaching of Limits on Ticket Sales Regulations).

## Self-healing

Triggered only by a selector miss on a page that loaded. Network errors, 5xx,
and auth walls route straight to FAILED; they are not selector problems and
asking a model about them just burns the latency budget.

The page is stripped to `id`, `class`, `role`, `aria-*`, `data-testid`, `type`
and text, with `<script>`, `<style>`, `<svg>`, comments, and every inline event
handler removed. The top candidates are validated **in-page** — exactly one
match, visible, enabled — before anything is adopted; an unvalidated selector is
never executed. On success the selector is written through to Postgres first
and then the Redis hash, so a crash between the two costs one query rather than
leaving a cached selector with no provenance row.

**One attempt.** Per drop, hard.

## Delivery semantics

At-least-once. `SET drop:claim:<drop_id> NX EX 900` is taken before any side
effect, including the INSERT into `drops`. `XAUTOCLAIM` reaps entries idle
longer than 60 s from dead consumers, and an entry delivered more than three
times is dead-lettered rather than left to block the group's recovery path.

A redelivery that finds the claim already held is **not** acked unless the drop
is provably terminal in Postgres. Acking unconditionally would silently discard
a drop whose owner died one instruction after taking the key.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests scripts
.\.venv\Scripts\ruff.exe format --check src tests scripts
```

No test in the suite opens a socket. The `Store`, `Bus`, `PageLike`, `Embedder`,
and `LLMClient` protocols exist so every stage can run against an in-memory
double, which is also what lets a test assert *ordering* — that the transition
row was written before the next node ran — rather than only final state.

Two properties are worth calling out because they are easy to break silently:

- `test_announced_message_can_never_route_to_reserving` sweeps the whole score
  range for a discovery message. The branch is the only thing keeping a browser
  away from a listings page.
- `test_transition_is_written_before_the_node_does_its_work` asserts the write
  ordering that makes a crashed worker's last state recoverable.

## End-to-end

`scripts/e2e_smoke.py` is deliberately outside the pytest suite because it
opens real sockets. It seeds the catalogue rows and preference vectors the Go
ingester would have written, publishes one availability-tier and one
discovery-tier message shaped exactly like `internal/stream/publisher.go`
emits, runs the real harness over them, asserts the resulting rows, and removes
everything it created.

```powershell
docker compose ps            # from the repo root; postgres and redis healthy
.\.venv\Scripts\python.exe scripts\e2e_smoke.py
```

Pass `--keep` to leave the rows in place for inspection.

`scripts/check_autoclaim.py` proves the recovery path against live Redis on a
throwaway stream: a consumer reads an entry and never acks it, and a second
consumer reclaims it with an incremented delivery count. The unit tests fake the
client, so this is the only place XAUTOCLAIM's real semantics are exercised —
worth having, because it is the sole recovery path for a worker that died
between XREADGROUP and XACK.

## Operational notes

- Watchlist promotion follows `event_links` onto the availability-tier
  counterpart. The ingest poller's due-items query requires the watchlisted
  event to belong to `watch_source`, so promoting an EDMTrain row under
  `edmtrain` would create a row nothing ever polls. When no link exists yet the
  promotion still lands on the discovered event (the alternative is silently
  losing the slot). A harness loop every `WATCHLIST_REPOINT_INTERVAL_MS`
  (default 30 s) rewrites those inert rows once the resolver catches up.
  Ingest never writes `watchlist`; that table is harness-owned.
- A state deadline expiry routes to FAILED and never retries. The deadlines are
  an order of magnitude above the §3 latency budget on purpose: the budget is
  what you alert on, the deadline is where you give up on a stage that is not
  coming back.
- `HARNESS_MAX_CONCURRENCY` is the ceiling on simultaneous drops. On a big drop
  every worker wants a browser context at the same instant, and this is the
  difference between a slow queue and an OOM kill.
- The harness synthesizes a stub `events` row if the catalogue is missing one,
  because `drops.event_id` is NOT NULL and there would otherwise be nowhere to
  record that the drop happened. Stubs are flagged in the transition detail and
  corrected by the ingester's next upsert.
