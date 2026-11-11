CREATE TABLE IF NOT EXISTS ingest_outbox (
    id              uuid PRIMARY KEY,
    event_id        uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    fingerprint     text NOT NULL,
    payload         jsonb NOT NULL,
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_until    timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS ingest_outbox_due_idx
    ON ingest_outbox (next_attempt_at, created_at)
    WHERE published_at IS NULL;
