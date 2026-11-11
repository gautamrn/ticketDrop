-- TicketDrop RA-only alert product schema.

CREATE TYPE drop_state AS ENUM ('NOTIFIED', 'SKIPPED', 'FAILED');
CREATE TYPE change_kind AS ENUM ('new_ticketed', 'tier_available', 'returned_to_sale');
CREATE TYPE preference_kind AS ENUM ('artist', 'venue');
CREATE TYPE watch_tier AS ENUM ('hot', 'warm', 'cold');
CREATE TYPE user_role AS ENUM ('user', 'operator');
CREATE TYPE notification_channel_kind AS ENUM ('email');
CREATE TYPE delivery_status AS ENUM ('pending', 'sending', 'sent', 'failed', 'suppressed');
CREATE TYPE subscription_origin AS ENUM ('explicit', 'scout');

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Catalogue: Go is the only normal writer. `raw` preserves upstream fields
-- when RA changes its undocumented GraphQL schema.
CREATE TABLE venues (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text NOT NULL DEFAULT 'ra' CHECK (source = 'ra'),
    source_venue_id text NOT NULL,
    name            text NOT NULL,
    area_id         integer,
    area_name       text,
    latitude        double precision,
    longitude       double precision,
    content_url     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_venue_id)
);

CREATE TABLE artists (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source           text NOT NULL DEFAULT 'ra' CHECK (source = 'ra'),
    source_artist_id text NOT NULL,
    name             text NOT NULL,
    slug             text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_artist_id)
);

CREATE TABLE events (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source           text NOT NULL DEFAULT 'ra' CHECK (source = 'ra'),
    source_event_id  text NOT NULL,
    title            text NOT NULL,
    content_url      text,
    ticket_link      text,
    venue_id         uuid REFERENCES venues (id) ON DELETE SET NULL,
    starts_at        timestamptz,
    ends_at          timestamptz,
    listing_date     date,
    is_ticketed      boolean NOT NULL DEFAULT false,
    queue_it_enabled boolean NOT NULL DEFAULT false,
    attending        integer,
    minimum_age      integer,
    genres           text[],
    raw              jsonb,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_event_id)
);

CREATE TABLE event_artists (
    event_id  uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    artist_id uuid NOT NULL REFERENCES artists (id) ON DELETE CASCADE,
    billing   smallint,
    PRIMARY KEY (event_id, artist_id)
);

CREATE TABLE ingest_watermarks (
    source     text PRIMARY KEY CHECK (source = 'ra'),
    cursor_at  timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One shared polling row per RA event, independent of subscriber count.
CREATE TABLE watchlist (
    event_id       uuid PRIMARY KEY REFERENCES events (id) ON DELETE CASCADE,
    tier           watch_tier NOT NULL DEFAULT 'hot',
    reason         text,
    active         boolean NOT NULL DEFAULT true,
    last_polled_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX watchlist_due_idx ON watchlist (tier, last_polled_at NULLS FIRST)
    WHERE active;

CREATE TABLE event_snapshots (
    id               bigserial PRIMARY KEY,
    event_id         uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    fingerprint      text NOT NULL,
    is_ticketed      boolean NOT NULL DEFAULT false,
    cost             text,
    presale_status   text,
    resale_active    boolean NOT NULL DEFAULT false,
    queue_it         boolean NOT NULL DEFAULT false,
    event_status     text,
    attending        integer,
    interested_count integer,
    date_updated     timestamptz,
    raw              jsonb,
    observed_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX event_snapshots_event_observed_idx
    ON event_snapshots (event_id, observed_at DESC);

-- A NULL state is in-flight. Only terminal outcomes are part of the state
-- vocabulary; the old browser/scoring graph no longer exists.
CREATE TABLE drops (
    id          uuid PRIMARY KEY,
    event_id    uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    change      change_kind NOT NULL,
    fingerprint text NOT NULL,
    state       drop_state,
    detected_at timestamptz NOT NULL,
    error       text,
    notified_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX drops_event_fingerprint_key ON drops (event_id, fingerprint);
CREATE INDEX drops_inflight_idx ON drops (created_at) WHERE state IS NULL;

CREATE TABLE drop_transitions (
    id         bigserial PRIMARY KEY,
    drop_id    uuid NOT NULL REFERENCES drops (id) ON DELETE CASCADE,
    from_state drop_state,
    to_state   drop_state NOT NULL,
    latency_ms integer,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX drop_transitions_drop_idx ON drop_transitions (drop_id, at);

-- Auth.js adapter tables retain its expected quoted column names.
CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text,
    email           text UNIQUE,
    "emailVerified" timestamptz,
    image           text,
    role            user_role NOT NULL DEFAULT 'user',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    "userId"            uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    type                text NOT NULL,
    provider            text NOT NULL,
    "providerAccountId" text NOT NULL,
    refresh_token       text,
    access_token        text,
    expires_at          bigint,
    token_type          text,
    scope               text,
    id_token            text,
    session_state       text,
    UNIQUE (provider, "providerAccountId")
);

CREATE TABLE sessions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    "sessionToken" text NOT NULL UNIQUE,
    "userId"       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires        timestamptz NOT NULL
);

CREATE TABLE verification_token (
    identifier text NOT NULL,
    token      text NOT NULL,
    expires    timestamptz NOT NULL,
    PRIMARY KEY (identifier, token)
);

CREATE TABLE user_settings (
    user_id      uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    ra_area_id   integer,
    ra_area_name text,
    demo_mode    boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_preferences (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    label      text NOT NULL CHECK (btrim(label) <> ''),
    kind       preference_kind NOT NULL,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX user_preferences_identity_key
    ON user_preferences (user_id, kind, lower(label));
CREATE INDEX user_preferences_user_idx
    ON user_preferences (user_id, active, kind);

CREATE TABLE event_subscriptions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    event_id      uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    active        boolean NOT NULL DEFAULT true,
    origin        subscription_origin NOT NULL,
    user_disabled boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, event_id)
);

CREATE INDEX event_subscriptions_active_event_idx
    ON event_subscriptions (event_id, user_id) WHERE active;
CREATE INDEX event_subscriptions_active_user_idx
    ON event_subscriptions (user_id, event_id) WHERE active;

CREATE TABLE notification_channels (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind        notification_channel_kind NOT NULL DEFAULT 'email',
    destination text NOT NULL,
    verified_at timestamptz,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, kind, destination)
);

CREATE TABLE notification_deliveries (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    drop_id             uuid NOT NULL REFERENCES drops (id) ON DELETE CASCADE,
    user_id             uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    channel_id          uuid NOT NULL REFERENCES notification_channels (id) ON DELETE CASCADE,
    channel             notification_channel_kind NOT NULL,
    status              delivery_status NOT NULL DEFAULT 'pending',
    attempts            smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    provider_message_id text,
    last_error          text,
    next_attempt_at     timestamptz,
    sent_at             timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (drop_id, user_id, channel)
);

CREATE INDEX notification_deliveries_due_idx
    ON notification_deliveries (next_attempt_at, created_at)
    WHERE status IN ('pending', 'failed');

CREATE TRIGGER venues_touch BEFORE UPDATE ON venues
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER artists_touch BEFORE UPDATE ON artists
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER events_touch BEFORE UPDATE ON events
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER watchlist_touch BEFORE UPDATE ON watchlist
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER drops_touch BEFORE UPDATE ON drops
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER user_settings_touch BEFORE UPDATE ON user_settings
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER user_preferences_touch BEFORE UPDATE ON user_preferences
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER event_subscriptions_touch BEFORE UPDATE ON event_subscriptions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER notification_channels_touch BEFORE UPDATE ON notification_channels
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER notification_deliveries_touch BEFORE UPDATE ON notification_deliveries
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
