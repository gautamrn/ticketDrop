// Package store owns all Postgres access for the ingest service.
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"ticketdrop/ingest/internal/source"
	"ticketdrop/ingest/internal/stream"
)

type Store struct{ pool *pgxpool.Pool }

func New(ctx context.Context, databaseURL string) (*Store, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("store: connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() { s.pool.Close() }

func (s *Store) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

// Watermark returns how far an adapter has consumed. A missing row is not an
// error: first run legitimately has no cursor.
func (s *Store) Watermark(ctx context.Context, src string) (time.Time, bool, error) {
	var at time.Time
	err := s.pool.QueryRow(ctx,
		`SELECT cursor_at FROM ingest_watermarks WHERE source = $1`, src,
	).Scan(&at)
	if errors.Is(err, pgx.ErrNoRows) {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, fmt.Errorf("store: read watermark: %w", err)
	}
	return at, true, nil
}

// SetWatermark advances the cursor. GREATEST makes it monotonic, so a late
// write from a slow poll cannot rewind progress made by a newer one.
func (s *Store) SetWatermark(ctx context.Context, src string, at time.Time) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO ingest_watermarks (source, cursor_at)
		VALUES ($1, $2)
		ON CONFLICT (source) DO UPDATE
		SET cursor_at  = GREATEST(ingest_watermarks.cursor_at, EXCLUDED.cursor_at),
		    updated_at = now()`, src, at)
	if err != nil {
		return fmt.Errorf("store: set watermark: %w", err)
	}
	return nil
}

// UpsertResult reports what happened to one event.
type UpsertResult struct {
	EventID string
	// IsNew distinguishes a first sighting from a refresh. It comes from
	// Postgres' xmax system column, which is zero only on a genuine insert.
	// Deriving it from the write itself avoids the check-then-set race that a
	// separate "have we seen this" lookup would introduce.
	IsNew bool
}

// UpsertEvent writes an event and its venue and lineup in one transaction.
//
// ON CONFLICT DO UPDATE rather than DO NOTHING: a show whose lineup or ticket
// link changes upstream must not stay stale in our copy just because we saw it
// once. DO NOTHING would make first write permanent.
func (s *Store) UpsertEvent(ctx context.Context, ev source.Event) (UpsertResult, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return UpsertResult{}, fmt.Errorf("store: begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var venueID *string
	if ev.Venue != nil {
		id, err := upsertVenue(ctx, tx, ev.Source, *ev.Venue)
		if err != nil {
			return UpsertResult{}, err
		}
		venueID = &id
	}

	var res UpsertResult
	err = tx.QueryRow(ctx, `
		INSERT INTO events (
			source, source_event_id, title, content_url, ticket_link, venue_id,
			starts_at, ends_at, listing_date, is_ticketed, attending,
			minimum_age, genres, raw
		) VALUES (
			$1, $2, $3, $4, $5, $6,
			$7, $8, $9, $10, $11,
			$12, $13, $14::jsonb
		)
		ON CONFLICT (source, source_event_id) DO UPDATE SET
			title        = EXCLUDED.title,
			content_url  = EXCLUDED.content_url,
			ticket_link  = EXCLUDED.ticket_link,
			venue_id     = COALESCE(EXCLUDED.venue_id, events.venue_id),
			starts_at    = EXCLUDED.starts_at,
			ends_at      = EXCLUDED.ends_at,
			listing_date = EXCLUDED.listing_date,
			is_ticketed  = EXCLUDED.is_ticketed,
			attending    = COALESCE(EXCLUDED.attending, events.attending),
			minimum_age  = COALESCE(EXCLUDED.minimum_age, events.minimum_age),
			genres       = EXCLUDED.genres,
			raw          = EXCLUDED.raw,
			last_seen_at = now()
		RETURNING id::text, (xmax = 0)`,
		ev.Source, ev.SourceEventID, ev.Title, nullStr(ev.ContentURL), nullStr(ev.TicketLink), venueID,
		ev.StartsAt, ev.EndsAt, ev.ListingDate, ev.IsTicketed, ev.Attending,
		ev.MinimumAge, ev.Genres, string(ev.Raw),
	).Scan(&res.EventID, &res.IsNew)
	if err != nil {
		return UpsertResult{}, fmt.Errorf("store: upsert event %s/%s: %w", ev.Source, ev.SourceEventID, err)
	}

	for i, ar := range ev.Artists {
		artistID, err := upsertArtist(ctx, tx, ev.Source, ar)
		if err != nil {
			return UpsertResult{}, err
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO event_artists (event_id, artist_id, billing)
			VALUES ($1, $2, $3)
			ON CONFLICT (event_id, artist_id) DO UPDATE
			SET billing = EXCLUDED.billing`,
			res.EventID, artistID, int16(i),
		); err != nil {
			return UpsertResult{}, fmt.Errorf("store: link artist: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return UpsertResult{}, fmt.Errorf("store: commit: %w", err)
	}
	return res, nil
}

// WatchItem is one event the availability loop polls, joined with the fields
// needed to build a stream message without a second query.
type WatchItem struct {
	EventID       string
	Source        string
	SourceEventID string
	Title         string
	ContentURL    string
	TicketLink    string
	VenueName     string
	StartsAt      *time.Time
	Tier          string
	// Artists travel with the watch item because artist affinity is the
	// heaviest term in the hype score (0.40). Leaving them out would force the
	// harness into a second round trip on the hot path, inside the 300 ms
	// evaluation budget, for data we already had in hand here.
	Artists []string
}

// DueWatchItems returns watchlisted events ordered least-recently-polled first.
//
// The source predicate is defensive; the compact schema permits only RA.
func (s *Store) DueWatchItems(ctx context.Context, sourceName string, limit int) ([]WatchItem, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT e.id::text, e.source::text, e.source_event_id,
		       e.title, COALESCE(e.content_url, ''), COALESCE(e.ticket_link, ''),
		       COALESCE(v.name, ''), e.starts_at, w.tier::text,
		       COALESCE((
		           SELECT array_agg(a.name ORDER BY ea.billing NULLS LAST, a.name)
		           FROM event_artists ea
		           JOIN artists a ON a.id = ea.artist_id
		           WHERE ea.event_id = e.id
		       ), '{}') AS artist_names
		FROM watchlist w
		JOIN events e ON e.id = w.event_id
		LEFT JOIN venues v ON v.id = e.venue_id
		WHERE w.active
		  AND e.source = $1
		ORDER BY w.last_polled_at NULLS FIRST
		LIMIT $2`, sourceName, limit)
	if err != nil {
		return nil, fmt.Errorf("store: due watch items: %w", err)
	}
	defer rows.Close()

	var out []WatchItem
	for rows.Next() {
		var w WatchItem
		if err := rows.Scan(&w.EventID, &w.Source, &w.SourceEventID, &w.Title,
			&w.ContentURL, &w.TicketLink, &w.VenueName, &w.StartsAt, &w.Tier,
			&w.Artists); err != nil {
			return nil, fmt.Errorf("store: scan watch item: %w", err)
		}
		out = append(out, w)
	}
	return out, rows.Err()
}

// MarkPolled records that an event was checked, which is what makes the
// least-recently-polled ordering rotate instead of starving the tail.
func (s *Store) MarkPolled(ctx context.Context, eventID string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE watchlist SET last_polled_at = now() WHERE event_id = $1`, eventID)
	if err != nil {
		return fmt.Errorf("store: mark polled: %w", err)
	}
	return nil
}

// RecordSnapshot appends a durable observation. Called only when the fingerprint
// changed, so this table is a history of transitions rather than of polls.
func (s *Store) RecordSnapshot(ctx context.Context, eventID, fp string, snap source.Snapshot) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO event_snapshots (
			event_id, fingerprint, is_ticketed, cost, presale_status,
			resale_active, queue_it, event_status, attending, interested_count,
			date_updated, raw, observed_at
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)`,
		eventID, fp, snap.IsTicketed, nullStr(snap.Cost), nullStr(snap.PresaleStatus),
		snap.ResaleActive, snap.QueueIt, nullStr(snap.EventStatus),
		snap.Attending, snap.InterestedCount, snap.DateUpdated,
		rawOrNull(snap.Raw), snap.ObservedAt)
	if err != nil {
		return fmt.Errorf("store: record snapshot: %w", err)
	}
	return nil
}

// RecordSnapshotAndEnqueueDrop commits the observed transition and its outbound
// message together. The cache may lag this transaction, so the uniqueness key
// makes a repeated observation return the existing outbox item.
func (s *Store) RecordSnapshotAndEnqueueDrop(
	ctx context.Context,
	eventID, fp string,
	snap source.Snapshot,
	drop stream.Drop,
) (bool, error) {
	payload, err := json.Marshal(drop)
	if err != nil {
		return false, fmt.Errorf("store: marshal outbox drop: %w", err)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, fmt.Errorf("store: begin snapshot outbox: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, `
		INSERT INTO event_snapshots (
			event_id, fingerprint, is_ticketed, cost, presale_status,
			resale_active, queue_it, event_status, attending, interested_count,
			date_updated, raw, observed_at
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)`,
		eventID, fp, snap.IsTicketed, nullStr(snap.Cost), nullStr(snap.PresaleStatus),
		snap.ResaleActive, snap.QueueIt, nullStr(snap.EventStatus),
		snap.Attending, snap.InterestedCount, snap.DateUpdated,
		rawOrNull(snap.Raw), snap.ObservedAt,
	); err != nil {
		return false, fmt.Errorf("store: record outbox snapshot: %w", err)
	}

	tag, err := tx.Exec(ctx, `
		INSERT INTO ingest_outbox (id, event_id, fingerprint, payload)
		VALUES ($1, $2, $3, $4::jsonb)
		ON CONFLICT (event_id, fingerprint) DO NOTHING`,
		drop.ID, eventID, fp, payload,
	)
	if err != nil {
		return false, fmt.Errorf("store: enqueue outbox drop: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return false, fmt.Errorf("store: commit snapshot outbox: %w", err)
	}
	return tag.RowsAffected() == 1, nil
}

type OutboxItem struct {
	ID       string
	Payload  []byte
	Attempts int
}

func (s *Store) ClaimOutbox(
	ctx context.Context,
	limit int,
	lease time.Duration,
) ([]OutboxItem, error) {
	rows, err := s.pool.Query(ctx, `
		WITH due AS (
			SELECT id
			FROM ingest_outbox
			WHERE published_at IS NULL
			  AND next_attempt_at <= now()
			  AND (locked_until IS NULL OR locked_until <= now())
			ORDER BY next_attempt_at, created_at
			LIMIT $1
			FOR UPDATE SKIP LOCKED
		)
		UPDATE ingest_outbox o
		SET locked_until = now() + make_interval(secs => $2),
		    attempts = attempts + 1,
		    last_error = NULL
		FROM due
		WHERE o.id = due.id
		RETURNING o.id::text, o.payload, o.attempts`,
		limit, int(lease.Seconds()),
	)
	if err != nil {
		return nil, fmt.Errorf("store: claim outbox: %w", err)
	}
	defer rows.Close()

	var items []OutboxItem
	for rows.Next() {
		var item OutboxItem
		if err := rows.Scan(&item.ID, &item.Payload, &item.Attempts); err != nil {
			return nil, fmt.Errorf("store: scan outbox: %w", err)
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *Store) MarkOutboxPublished(ctx context.Context, id string) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE ingest_outbox
		SET published_at = now(), locked_until = NULL, last_error = NULL
		WHERE id = $1`, id)
	if err != nil {
		return fmt.Errorf("store: mark outbox published: %w", err)
	}
	return nil
}

func (s *Store) RetryOutbox(
	ctx context.Context,
	id, reason string,
	delay time.Duration,
) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE ingest_outbox
		SET locked_until = NULL,
		    next_attempt_at = now() + make_interval(secs => $2),
		    last_error = $3
		WHERE id = $1`, id, int(delay.Seconds()), reason)
	if err != nil {
		return fmt.Errorf("store: retry outbox: %w", err)
	}
	return nil
}

// SeedWatchlist is a development helper: it watchlists the most-saved events for
// a source so the availability loop has something to poll before the harness
// reconciler runs. Not part of the production path.
func (s *Store) SeedWatchlist(ctx context.Context, src string, limit int) (int, error) {
	tag, err := s.pool.Exec(ctx, `
		INSERT INTO watchlist (event_id, tier, reason)
		SELECT id, 'hot', 'dev seed: top by attending'
		FROM events
		WHERE source = $1
		ORDER BY attending DESC NULLS LAST
		LIMIT $2
		ON CONFLICT (event_id) DO NOTHING`, src, limit)
	if err != nil {
		return 0, fmt.Errorf("store: seed watchlist: %w", err)
	}
	return int(tag.RowsAffected()), nil
}

func rawOrNull(raw []byte) string {
	if len(raw) == 0 {
		return "null"
	}
	return string(raw)
}

func upsertVenue(ctx context.Context, tx pgx.Tx, src string, v source.Venue) (string, error) {
	var id string
	err := tx.QueryRow(ctx, `
		INSERT INTO venues (source, source_venue_id, name, area_id, area_name, latitude, longitude, content_url)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		ON CONFLICT (source, source_venue_id) DO UPDATE SET
			name        = EXCLUDED.name,
			area_id     = COALESCE(EXCLUDED.area_id, venues.area_id),
			area_name   = COALESCE(EXCLUDED.area_name, venues.area_name),
			latitude    = COALESCE(EXCLUDED.latitude, venues.latitude),
			longitude   = COALESCE(EXCLUDED.longitude, venues.longitude),
			content_url = COALESCE(EXCLUDED.content_url, venues.content_url)
		RETURNING id::text`,
		src, v.SourceVenueID, v.Name, v.AreaID, nullStr(v.AreaName), v.Latitude, v.Longitude, nullStr(v.ContentURL),
	).Scan(&id)
	if err != nil {
		return "", fmt.Errorf("store: upsert venue %q: %w", v.Name, err)
	}
	return id, nil
}

func upsertArtist(ctx context.Context, tx pgx.Tx, src string, a source.Artist) (string, error) {
	var id string
	err := tx.QueryRow(ctx, `
		INSERT INTO artists (source, source_artist_id, name, slug)
		VALUES ($1, $2, $3, $4)
		ON CONFLICT (source, source_artist_id) DO UPDATE SET
			name = EXCLUDED.name,
			slug = COALESCE(EXCLUDED.slug, artists.slug)
		RETURNING id::text`,
		src, a.SourceArtistID, a.Name, nullStr(a.ContentURL),
	).Scan(&id)
	if err != nil {
		return "", fmt.Errorf("store: upsert artist %q: %w", a.Name, err)
	}
	return id, nil
}

func nullStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
