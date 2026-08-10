// Package store owns all Postgres access for the ingest service.
package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"ticketdrop/ingest/internal/source"
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

// Watermark returns how far an adapter has consumed. A missing row is not an
// error: first run legitimately has no cursor.
func (s *Store) Watermark(ctx context.Context, src string) (time.Time, bool, error) {
	var at time.Time
	err := s.pool.QueryRow(ctx,
		`SELECT cursor_at FROM ingest_watermarks WHERE source = $1::source_platform`, src,
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
		VALUES ($1::source_platform, $2)
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
			minimum_age, genres, announced_at, raw
		) VALUES (
			$1::source_platform, $2, $3, $4, $5, $6,
			$7, $8, $9, $10, $11,
			$12, $13, $14, $15::jsonb
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
			-- Keep the earliest announcement time we ever saw.
			announced_at = COALESCE(events.announced_at, EXCLUDED.announced_at),
			raw          = EXCLUDED.raw,
			last_seen_at = now()
		RETURNING id::text, (xmax = 0)`,
		ev.Source, ev.SourceEventID, ev.Title, nullStr(ev.ContentURL), nullStr(ev.TicketLink), venueID,
		ev.StartsAt, ev.EndsAt, ev.ListingDate, ev.IsTicketed, ev.Attending,
		ev.MinimumAge, ev.Genres, ev.AnnouncedAt, string(ev.Raw),
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
			INSERT INTO event_artists (event_id, artist_id, billing, b2b_with_next)
			VALUES ($1, $2, $3, $4)
			ON CONFLICT (event_id, artist_id) DO UPDATE
			SET billing = EXCLUDED.billing, b2b_with_next = EXCLUDED.b2b_with_next`,
			res.EventID, artistID, int16(i), ar.B2BWithNext,
		); err != nil {
			return UpsertResult{}, fmt.Errorf("store: link artist: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return UpsertResult{}, fmt.Errorf("store: commit: %w", err)
	}
	return res, nil
}

func upsertVenue(ctx context.Context, tx pgx.Tx, src string, v source.Venue) (string, error) {
	var id string
	err := tx.QueryRow(ctx, `
		INSERT INTO venues (source, source_venue_id, name, area_name, latitude, longitude, content_url)
		VALUES ($1::source_platform, $2, $3, $4, $5, $6, $7)
		ON CONFLICT (source, source_venue_id) DO UPDATE SET
			name        = EXCLUDED.name,
			area_name   = COALESCE(EXCLUDED.area_name, venues.area_name),
			latitude    = COALESCE(EXCLUDED.latitude, venues.latitude),
			longitude   = COALESCE(EXCLUDED.longitude, venues.longitude),
			content_url = COALESCE(EXCLUDED.content_url, venues.content_url)
		RETURNING id::text`,
		src, v.SourceVenueID, v.Name, nullStr(v.AreaName), v.Latitude, v.Longitude, nullStr(v.ContentURL),
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
		VALUES ($1::source_platform, $2, $3, $4)
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
