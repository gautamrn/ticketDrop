# TicketDrop option 1 — implementation handoff

Execute this spec in order. Do not preserve a deleted subsystem "for later." There are no production users and no production database. A local Docker volume may be destroyed with `docker compose down -v`.

The git working tree is the baseline, not `HEAD`. `HEAD` is `8461476` ("cleanedup + forgot to commit everything") and does not contain the multi-user app. Do not revert uncommitted files this spec tells you to keep. Do not commit `.env`.

## Product

A user follows artist names and one Resident Advisor metro. The ingester enumerates that metro's public RA catalogue and fast-polls a shared watchlist. When a followed artist's show appears in the metro, the user is subscribed automatically. When a later poll classifies a sale-state change, each active subscriber gets one email with RA's event link and the outbound vendor link.

RA does not sell these tickets. Sampled events use `ticketingSystem: LEGACY`. Unauthenticated `Event.tickets` is an empty array. The alert means one of these listing fields changed:

- `presaleStatus` null → `ACTIVE`, stored as `tier_available`. A presale opening outranks a cost change on the same poll.
- `isTicketed` false → true, stored as `new_ticketed`.
- `resaleActive` false → true, stored as `returned_to_sale`.
- normalized `cost` text changed while the event is ticketed, stored as `tier_available`.

`dateUpdated` is in the fingerprint so an unknown field change is visible, and it never classifies a drop by itself. `attending` and `interestedCount` stay out of the hash. The first observation of an event writes a baseline and emits nothing. A fingerprint delta that `Classify` cannot explain is not emailed.

`queueItEnabled` still allows the email. The body says a waiting room is present. Nothing navigates it.

This is an alert product. It does not buy tickets, solve a CAPTCHA, enter a queue, or evade a purchase limit. US BOTS Act and UK Breaching of Limits on Ticket Sales Regulations 2018 are the reason. EDMTrain is removed; its terms restrict combining its listings with another source in a public discovery product, and option 1 does not need it.

Songkick already sends announcement mail. This product's job is the later onsale signal plus the vendor link. Do not email merely because a show was added to the catalogue. The in-app list shows shows now being watched. Email fires only for a classified change.

## Keep these invariants

- Go is the only writer of the catalogue and the only publisher to Redis stream `ticket:drops`. Python never polls RA.
- Python is the only writer of `watchlist`, `event_subscriptions`, `drops`, `drop_transitions`, and `notification_deliveries`.
- Postgres is the durable record. Redis holds the stream, the poll snapshot cache (`snap:ra:<event_id>`), and `SET drop:claim:<drop_id> NX EX 900`. Losing Redis may repeat work. It must not create a second email or a second drop row.
- Delivery is at-least-once. Unique `(event_id, fingerprint)` on `drops` and unique `(drop_id, user_id, channel)` on `notification_deliveries`. Resend receives the same `Idempotency-Key` on every retry: `drop-<id>-user-<id>-email`.
- A user Unwatch sets `active = false` and `user_disabled = true`. The scout must not turn that row back on.
- Explicit Watch still works and counts toward the 25-active-subscription cap. The scout does not evict an explicit watch to make room.
- One shared `watchlist` row per RA event, no matter how many subscribers. Source load follows unique events.
- Stub `events` insert stays: if a stream message names a missing event, Python inserts `ON CONFLICT DO UPDATE`, logs a warning, and records `stubbed_event` on the transition. Go's next upsert corrects it.
- Secrets come from the environment. No key, cookie, or token is inlined.
- Poll interval stays configuration. The availability floor remains 500 ms. Production starts at 5 s, 6 workers, one metro.
- Every blocking call keeps a timeout.

## Delete

Remove the code, tests, dependencies, env vars, docs, and schema for these. Do not leave a feature flag.

**EDMTrain and cross-source identity**

- `ingest-service/internal/source/edmtrain/`
- `ingest-service/cmd/mock-edmtrain/`
- `ingest-service/internal/pipeline/discovery.go`
- `ingest-service/internal/pipeline/resolver.go`
- `ingest-service/internal/resolve/`
- `PlatformEDMTrain`, `TierDiscovery`, `DiscoveryAdapter` if nothing remains to implement them
- `event_links` and the repoint loop (`repoint_inert_watchlist`, `WATCHLIST_REPOINT_INTERVAL_MS`)
- Admin links API and `LinksPanel`: `admin-panel/src/app/api/links/route.ts` and the links portion of `admin-panel/src/components/ControlPanels.tsx`

**Browser assist, LLM repair, LangGraph, vectors**

- `agent-harness/src/harness/browser/`
- `agent-harness/src/harness/healing/`
- `agent-harness/src/harness/fsm/` — replace with a straight worker, not a smaller graph
- `agent-harness/src/harness/scoring.py`
- `agent-harness/src/harness/embeddings.py`
- `agent-harness/src/harness/prefs.py` embedding path
- Playwright, LangGraph, Anthropic, BeautifulSoup, lxml, and numpy dependencies in `agent-harness/pyproject.toml`
- Voyage, Anthropic, `RESERVATION_MODE`, self-heal, hype-weight, and vector env vars
- `selector_overrides` table and Redis hash
- `user_drop_evaluations` and `preference_requests`. Artist names need no embedding, so Next.js inserts `user_preferences` directly
- `pgvector` extension, `vector` columns, HNSW indexes, `sound_signature`
- Drop states `EVALUATING`, `RESERVING`, `SELF_HEAL`. Remaining states: `NOTIFIED`, `SKIPPED`, `FAILED`
- Change kind `announced`
- Result stream `ticket:drops:results` and `admin-panel/src/app/api/stream/route.ts`. The operator page reads Postgres. One blocking Redis connection per browser goes away with it
- Hype score columns, preference weight as a ranking input, and UI that explains a score

**Tests and docs that only exist for the deleted path**

- `agent-harness/tests/test_dom.py`, `test_reserve.py`, `test_healing.py`, `test_embeddings.py`, `test_scoring.py`, `test_graph.py`, `test_watchlist.py`
- Rewrite `test_fsm_nodes.py` as worker tests. Keep the behaviors below; delete route tests for reserving and self-heal
- `agent-harness/scripts/e2e_smoke.py` and `check_autoclaim.py` if they encode the old graph. Port the claim and redelivery assertions into pytest
- Rewrite `ARCHITECTURE.md`, `README.md`, `ingest-service/README.md`, `agent-harness/README.md`, `admin-panel/README.md`, `docs/portfolio.md`, `docs/demo-script.md`, and `docs/launch.md` so they describe this product
- Update `.cursor/rules/system-arch.mdc`. It currently requires LangGraph, Playwright, and "the panel must not embed or add a preference." After this change the panel stores an artist name. The rule must match the code or the next edit will resurrect the deleted design

## Schema

Replace `db/init/001_schema.sql` with the compact schema. Delete `db/migrations/002_public_product.sql`. Do not add a third migration that leaves dead tables in the bootstrap.

Keep Auth.js tables `users`, `accounts`, `sessions`, `verification_token` with its expected quoted column names (`emailVerified`, `userId`, `sessionToken`, `providerAccountId`).

Keep:

- `venues`, `artists`, `events`, `event_artists`, `ingest_watermarks`, `event_snapshots`, `watchlist`
- `users.role` (`user` | `operator`)
- `user_settings(user_id, ra_area_id, ra_area_name, demo_mode)`
- `user_preferences(user_id, label, kind)` where `kind` is `artist` or `venue`. Unique `(user_id, kind, lower(label))`. No embedding, no weight
- `event_subscriptions(user_id, event_id, active, origin, user_disabled)`. `origin` is `explicit` or `scout`. Unique `(user_id, event_id)`. Index active pairs
- `drops(id, event_id, change, fingerprint, state, detected_at, error, notified_at)` plus unique `(event_id, fingerprint)`
- `drop_transitions(drop_id, from_state, to_state, latency_ms, detail, at)`
- `notification_channels` and `notification_deliveries` as they exist now, including the due index and the delivery uniqueness key
- `raw jsonb` on `events` and `event_snapshots`

`events.source` is constrained to `ra`. Drop `source_platform` if it would only contain one label, and use `text` plus `CHECK (source = 'ra')` on catalogue rows. `watch_source` on `watchlist` can be removed; every watched row is RA.

Remove `hype_score`, `attempt_count`, `reservation_url` unless `reservation_url` is still the vendor link. Store the vendor link on `events.ticket_link` and the RA URL on `events.content_url`. The email reads those columns.

## Scout

Add `agent-harness/src/harness/scout.py`, started from `app.py` beside the existing subscription reconciler.

Every 60 seconds, with a 5-second statement timeout:

1. Read users who have `ra_area_id` set and at least one active artist or venue preference.
2. Find RA events in that area with `starts_at >= now() - interval '1 day'` whose lineup contains a case-insensitive, whitespace-collapsed artist label, or whose venue name matches a venue preference the same way.
3. Insert a subscription with `origin = 'scout'` and `active = true` when the user has fewer than 25 active subscriptions.
4. `ON CONFLICT DO NOTHING` when `user_disabled` is already true. Never update `user_disabled` or reactivate those rows.
5. Leave the existing `reconcile_subscriptions` loop to project active subscriptions onto one `watchlist` row per event. The scout does not write `watchlist` itself, so there is one writer.

Match the normalized full name. `Prospa` matches `prospa`. It does not match `Prospa (Live)` and it does not fuzzy-match a different artist. That false watch is worse than a missed nickname.

The deployment's `RA_AREAS` is the allowed metro list. The settings UI offers only those areas. Do not make Go read `user_settings`. A user who picks an area the ingester does not enumerate gets an empty catalogue, which the UI states.

Cap and pause behavior:

- Manual Watch inserts `origin = 'explicit'`, `user_disabled = false`, subject to the same cap of 25.
- Unwatch sets `active = false`, `user_disabled = true` for both origins.
- Scout skips users already at 25. It does not delete or rank existing rows.

## Worker

Replace the graph with one function in `worker.py`:

1. `XREADGROUP` / `XAUTOCLAIM`. Reclaim idle entries after 60 seconds. Dead-letter after 3 deliveries by acknowledging and inserting a `FAILED` audit row when a `drop_id` exists.
2. Parse the existing flat string message from `ingest-service/internal/stream/publisher.go`. Malformed messages are dead-lettered.
3. `SET drop:claim:<drop_id> NX EX 900`. If the claim exists and the drop is terminal, ack. If it is non-terminal, leave the entry pending.
4. `ensure_event`, then insert `drops`. Primary-key conflict on a terminal drop acks. `(event_id, fingerprint)` conflict acks as a duplicate poll.
5. Record one `drop_transitions` row with queue lag in `latency_ms` and `detail.stubbed_event`.
6. Load active subscribers for that event who have an active verified email channel. No subscribers → `SKIPPED`, ack.
7. For each subscriber, insert the delivery row, send email, mark sent or failed. Fan out concurrently. One subscriber's failure is logged on that delivery row and does not fail the others. The drop becomes `NOTIFIED` after the fan-out attempt, including when an individual email failed and is recorded as `failed`. The unique delivery key prevents a redelivery from sending again after `sent`.
8. Retry transient Resend errors (`429`, `5xx`, network) up to 3 times with bounded backoff, same idempotency key. A permanent `4xx` is one attempt.
9. Ack only after the drop is terminal.

Delete hype gating. An active subscription is the request for the alert.

Keep the claim, duplicate-fingerprint, dead-letter, and redelivery tests. Add a test that a `user_disabled` subscription is not recreated by the scout and does not receive mail.

## Email and UI

Email subject: `TicketDrop: <title>`. Body: the classified change in plain language, metro and venue, start time, RA `content_url` unmodified, `ticket_link` unmodified when present, and the queue-room sentence when needed. Keep the HMAC unsubscribe URL. Alert email remains the OAuth address; do not accept an arbitrary destination and mark it verified.

UI copy says "onsale signal," not "ticket drop" and not "hype."

- `/` explains: follow artists in a metro, one poll per show, email when RA's sale state changes, link to the seller.
- `/app/settings` saves metro and artist/venue names.
- `/app/events` lists the enumerated catalogue, Watch / Unwatch, and a badge for scout versus explicit.
- `/app/alerts` lists classified changes, delivery status, both links, and a short field-level detail from `drop_transitions.detail`. No score.
- `/admin` stays operator-only: Postgres-backed recent drops, health counts, pause/resume a shared watch. Delete the live SSE feed and the cross-source link queue.
- `/api/demo/replay` stays behind `PUBLIC_DEMO_MODE=true` and labels rows `[Demo]`. It must not run in the production profile.

## Ingest

Leave the RA GraphQL client, enumerator, fingerprint, snapshot cache, bounded worker pool, and stream publisher. Enumerator still emits no stream messages. Availability still emits only a classified change after a baseline exists.

Delete the EDMTrain branch and resolver from `cmd/ingest/main.go`. `RA_DEV_SEED_WATCHLIST` can remain as a dev-only seed; production scout replaces it.

Go tests stay table-driven. Fingerprint tests stay, including "attending is not part of the hash," "first sight emits nothing," and "unclassified delta does not publish."

## Runtime and deploy

`docker-compose.prod.yml` remains one VPS: private Postgres and Redis, Go, Python, Next.js, public Caddy only. Remove Voyage, Anthropic, and Playwright from the image and env. Python image stays `python:3.12-slim` with no browser install. Resend, Auth.js, and `UNSUBSCRIBE_SECRET` stay required in production.

`docker-compose.verify.yml` stays the integration harness. Update `scripts/product_smoke.py`:

1. Fresh schema, mock RA, ingest, harness.
2. Insert a user, Detroit area `19`, artist preference `Prospa`, and a verified email channel.
3. Wait until mock event `90001` has one active scout subscription and one active watchlist row.
4. Assert `drops` is still empty after the baseline poll.
5. `POST /admin/presale?id=90001`.
6. Assert one drop, four is no longer the expected transition count. Assert one `NOTIFIED` drop, one transition recording the classified change, and one `sent` delivery.
7. Run the presale mutation again and assert the delivery count is still 1.

Re-run `scripts/load_test.py` only after the worker is simplified. Replace `docs/performance.md`. The previous 100-drop result (p50 1.70 s, p95 2.67 s, p99 2.84 s, 4 workers, log email, WSL2) measured the deleted scoring pipeline and must not be cited.

## Docs the next editor must leave true

`ARCHITECTURE.md` should be short enough to defend:

- RA field limits and the fingerprint.
- Why the first sight is silent.
- Why the watchlist is shared.
- Why Redis delivery is at-least-once and Postgres keys make email idempotent.
- Alert-only legal boundary.
- The 5-second poll dominates detection delay. Stream-to-email latency is a separate measurement.
- Failure table: RA schema drift (`raw` jsonb), IP block (slow interval, few workers, one metro), poison message (dead-letter), Redis loss (baseline plus claim), duplicate subscribers (unique watch and unique delivery).

`.cursor/rules/system-arch.mdc` should describe the services that remain. Delete the requirement that Python use LangGraph or Playwright.

## Out of scope

Do not add EDMTrain, a second seller, page fetches of Dice or venue sites, push notifications, payments, fuzzy artist matching, sound analysis, or automated checkout. Those are different products.

## Done when

- `go test ./...`, `ruff`, `pytest`, and the Next.js lint, typecheck, and build pass.
- `docker compose -f docker-compose.verify.yml up --build --abort-on-container-exit --exit-code-from verify` passes the scout smoke above.
- A search of the repo finds no Playwright, LangGraph, Voyage, Anthropic, EDMTrain, `hype_score`, `selector_overrides`, or `event_links` references outside this handoff and git history.
- Production compose config still validates with `.env.production.example`.
