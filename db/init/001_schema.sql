-- TicketDrop schema.
--
-- Vector dimension is 1024 to match Voyage AI `voyage-3`. Claude has no
-- embeddings endpoint. Changing models means changing this dimension and
-- reindexing; pgvector fixes dimensionality at column-creation time.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TYPE source_platform AS ENUM ('ra', 'dice', 'edmtrain');

CREATE TYPE drop_state AS ENUM (
    'INGESTED',
    'EVALUATING',
    'RESERVING',
    'SELF_HEAL',
    'NOTIFIED',
    'SKIPPED',
    'FAILED'
);

CREATE TYPE tier_state AS ENUM ('available', 'sold_out', 'off_sale', 'unknown');

-- 'announced' comes from the discovery tier (EDMTrain createdDate) and means
-- "this show now exists", not "tickets are buyable". The other three come from
-- the availability tier and mean a real sale-state change.
CREATE TYPE change_kind AS ENUM (
    'announced',
    'new_ticketed',
    'tier_available',
    'returned_to_sale'
);

CREATE TYPE preference_kind AS ENUM ('artist', 'venue', 'sound', 'freeform');

-- Poll cadence class for the availability tier.
CREATE TYPE watch_tier AS ENUM ('hot', 'warm', 'cold');


CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------- catalogue

CREATE TABLE venues (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source          source_platform NOT NULL,
    source_venue_id text            NOT NULL,
    name            text            NOT NULL,
    area_id         integer,
    area_name       text,
    capacity        integer,
    latitude        double precision,
    longitude       double precision,
    content_url     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_venue_id)
);

CREATE TABLE artists (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source             source_platform NOT NULL,
    source_artist_id   text            NOT NULL,
    name               text            NOT NULL,
    slug               text,
    followers          integer,
    -- Semantic embedding of the artist's bio/genre text.
    semantic_embedding vector(1024),
    -- Numeric audio fingerprint: bpm band, key as sin/cos, energy,
    -- danceability, valence, acousticness, ... See docs in ARCHITECTURE.md §6.
    sound_signature    vector(12),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_artist_id)
);

CREATE TABLE events (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source             source_platform NOT NULL,
    source_event_id    text            NOT NULL,
    title              text            NOT NULL,
    content_url        text,
    venue_id           uuid REFERENCES venues (id) ON DELETE SET NULL,
    starts_at          timestamptz,
    ends_at            timestamptz,
    listing_date       date,
    is_ticketed        boolean NOT NULL DEFAULT false,
    -- RA flags events sitting behind a Queue-it waiting room. Such events are
    -- forced to alert-only regardless of RESERVATION_MODE.
    queue_it_enabled   boolean NOT NULL DEFAULT false,
    attending          integer,
    cost               text,
    minimum_age        integer,
    genres             text[],
    flyer_url          text,
    -- Outbound link to whichever vendor actually sells the ticket. EDMTrain is
    -- a listings site, so this is often the only way to reach a sale page.
    -- EDMTrain's ToS requires displaying its event link unmodified.
    ticket_link        text,
    -- When the show became publicly known (EDMTrain createdDate). Distinct from
    -- first_seen_at, which is when *we* first saw it.
    announced_at       timestamptz,
    semantic_embedding vector(1024),
    -- Unparsed source payload, so an upstream schema change is a backfill
    -- rather than permanent data loss.
    raw                jsonb,
    first_seen_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at       timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_event_id)
);

-- Entity resolution across sources. The same show is a separate row per
-- platform because each has its own id, fields, and refresh cadence; merging
-- on ingest would mean destructively guessing. Instead every duplicate points
-- at one elected canonical row, so a bad match is one UPDATE to undo.
-- Matching is fuzzy (normalized title + date + venue proximity) and therefore
-- carries a confidence score for review in the admin panel.
CREATE TABLE event_links (
    event_id           uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    canonical_event_id uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    match_confidence   real,
    match_method       text,
    confirmed          boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id),
    CHECK (event_id <> canonical_event_id)
);

CREATE INDEX event_links_canonical_idx ON event_links (canonical_event_id);

CREATE TABLE event_artists (
    event_id      uuid NOT NULL REFERENCES events (id)  ON DELETE CASCADE,
    artist_id     uuid NOT NULL REFERENCES artists (id) ON DELETE CASCADE,
    billing       smallint,
    -- EDMTrain marks an artist as performing back-to-back with the next artist
    -- in the lineup. A b2b set is a stronger hype signal than either name alone.
    b2b_with_next boolean NOT NULL DEFAULT false,
    PRIMARY KEY (event_id, artist_id)
);


-- ------------------------------------------------------------ availability

-- Ingestion is two-tier. The discovery tier (EDMTrain, ~60 s) answers "what
-- shows exist"; the availability tier (RA, ~2 s) answers "did the sale state
-- change". Polling every known event at 2 s would blow through every rate
-- limit we have, so only watchlisted events get the fast loop, and hype score
-- decides what earns a slot.
CREATE TABLE watchlist (
    event_id       uuid PRIMARY KEY REFERENCES events (id) ON DELETE CASCADE,
    tier           watch_tier NOT NULL DEFAULT 'warm',
    -- Which adapter polls availability. May differ from the source that
    -- discovered the event: EDMTrain finds it, RA watches it.
    watch_source   source_platform NOT NULL,
    watch_url      text,
    reason         text,
    active         boolean NOT NULL DEFAULT true,
    last_polled_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Scheduler query: least-recently-polled first, terminal rows excluded.
CREATE INDEX watchlist_due_idx ON watchlist (tier, last_polled_at NULLS FIRST)
    WHERE active;

-- Append-only observation log. Each poll that changes a tier writes a row;
-- the diff between consecutive rows is what defines a drop.
CREATE TABLE ticket_tiers (
    id           bigserial PRIMARY KEY,
    event_id     uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    name         text NOT NULL,
    state        tier_state NOT NULL DEFAULT 'unknown',
    price_minor  integer,
    currency     char(3),
    on_sale_at   timestamptz,
    raw          jsonb,
    observed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ticket_tiers_event_observed_idx
    ON ticket_tiers (event_id, observed_at DESC);


-- ---------------------------------------------------------------- FSM state

CREATE TABLE drops (
    id               uuid PRIMARY KEY,          -- uuid v7 minted by the Go ingester
    event_id         uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    change           change_kind NOT NULL,
    fingerprint      text        NOT NULL,
    state            drop_state  NOT NULL DEFAULT 'INGESTED',
    hype_score       numeric(4, 3) CHECK (hype_score >= 0 AND hype_score <= 1),
    attempt_count    smallint    NOT NULL DEFAULT 0,
    reservation_url  text,
    trace_id         text,
    error            text,
    detected_at      timestamptz NOT NULL,
    state_entered_at timestamptz NOT NULL DEFAULT now(),
    notified_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Dedupe guard: the same availability state for the same event is one drop,
-- however many times the poll loop sees it.
CREATE UNIQUE INDEX drops_event_fingerprint_key ON drops (event_id, fingerprint);

-- Supports "what is still in flight" without scanning terminal rows.
CREATE INDEX drops_active_idx ON drops (state, state_entered_at)
    WHERE state NOT IN ('NOTIFIED', 'SKIPPED', 'FAILED');

CREATE TABLE drop_transitions (
    id         bigserial PRIMARY KEY,
    drop_id    uuid NOT NULL REFERENCES drops (id) ON DELETE CASCADE,
    from_state drop_state,
    to_state   drop_state NOT NULL,
    latency_ms integer,
    detail     jsonb,
    at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX drop_transitions_drop_idx ON drop_transitions (drop_id, at);


-- -------------------------------------------------------------- preferences

CREATE TABLE user_preferences (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label           text NOT NULL,
    kind            preference_kind NOT NULL,
    embedding       vector(1024),
    sound_signature vector(12),
    weight          real NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------- self-healing

-- Durable mirror of selector_overrides:<domain> in Redis. Redis is the hot
-- cache; this survives a flush and carries the provenance needed to audit a
-- selector that an LLM inferred.
CREATE TABLE selector_overrides (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain       text NOT NULL,
    logical_name text NOT NULL,
    selector     text NOT NULL,
    confidence   real,
    model        text,
    healed_from  text,
    verified_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (domain, logical_name)
);


-- ------------------------------------------------------------- ANN indexes

-- HNSW rather than IVFFlat: IVFFlat must be trained on a populated table and
-- degrades as the corpus outgrows its centroids. The preference set is edited
-- live from the admin panel, so incremental index maintenance matters more
-- than build time.
CREATE INDEX artists_semantic_hnsw ON artists
    USING hnsw (semantic_embedding vector_cosine_ops);
CREATE INDEX artists_sound_hnsw ON artists
    USING hnsw (sound_signature vector_cosine_ops);
CREATE INDEX events_semantic_hnsw ON events
    USING hnsw (semantic_embedding vector_cosine_ops);
CREATE INDEX user_preferences_embedding_hnsw ON user_preferences
    USING hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------- triggers

CREATE TRIGGER venues_touch  BEFORE UPDATE ON venues
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER artists_touch BEFORE UPDATE ON artists
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER events_touch  BEFORE UPDATE ON events
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER drops_touch   BEFORE UPDATE ON drops
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER watchlist_touch BEFORE UPDATE ON watchlist
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER user_preferences_touch BEFORE UPDATE ON user_preferences
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER selector_overrides_touch BEFORE UPDATE ON selector_overrides
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
