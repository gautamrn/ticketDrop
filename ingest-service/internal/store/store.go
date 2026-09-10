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
// The join constrains to events belonging to watch_source. An event discovered
// on EDMTrain cannot be polled on RA using its EDMTrain id, so cross-source
// promotion requires resolving the show to an RA event row first. Unresolved
// rows remain durable but are not returned, avoiding upstream lookups that
// would 404 until the resolver links them.
func (s *Store) DueWatchItems(ctx context.Context, watchSource string, limit int) ([]WatchItem, error) {
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
		  AND w.watch_source = $1::source_platform
		  AND e.source = $1::source_platform
		ORDER BY w.last_polled_at NULLS FIRST
		LIMIT $2`, watchSource, limit)
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

// LinkTarget is an event considered for cross-source linking, carrying the
// fields the matcher scores on.
type LinkTarget struct {
	EventID       string
	SourceEventID string
	Title         string
	VenueName     string
	StartsAt      time.Time
	Artists       []string
}

const linkTargetColumns = `
	e.id::text, e.source_event_id, e.title,
	COALESCE(v.name, ''), COALESCE(e.starts_at, e.listing_date),
	COALESCE((
	    SELECT array_agg(a.name ORDER BY ea.billing NULLS LAST, a.name)
	    FROM event_artists ea
	    JOIN artists a ON a.id = ea.artist_id
	    WHERE ea.event_id = e.id
	), '{}')`

// UnlinkedEvents returns upcoming events from `src` that have no cross-source
// link yet, oldest-discovered first.
func (s *Store) UnlinkedEvents(ctx context.Context, src string, limit int) ([]LinkTarget, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT `+linkTargetColumns+`
		FROM events e
		LEFT JOIN venues v ON v.id = e.venue_id
		WHERE e.source = $1::source_platform
		  AND COALESCE(e.starts_at, e.listing_date) > now()
		  AND NOT EXISTS (SELECT 1 FROM event_links l WHERE l.event_id = e.id)
		ORDER BY e.created_at
		LIMIT $2`, src, limit)
	if err != nil {
		return nil, fmt.Errorf("store: unlinked events: %w", err)
	}
	return scanLinkTargets(rows)
}

// LinkCandidates returns events from `src` scheduled near `around`.
//
// Candidates come from our own tables rather than a fresh upstream query. The
// enumerate loop has already stored every RA event in the configured areas, so
// resolution costs one local query instead of one API call per unresolved event.
func (s *Store) LinkCandidates(ctx context.Context, src string, around time.Time, slack time.Duration) ([]LinkTarget, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT `+linkTargetColumns+`
		FROM events e
		LEFT JOIN venues v ON v.id = e.venue_id
		WHERE e.source = $1::source_platform
		  AND COALESCE(e.starts_at, e.listing_date) BETWEEN $2 AND $3`,
		src, around.Add(-slack), around.Add(slack))
	if err != nil {
		return nil, fmt.Errorf("store: link candidates: %w", err)
	}
	return scanLinkTargets(rows)
}

func scanLinkTargets(rows pgx.Rows) ([]LinkTarget, error) {
	defer rows.Close()

	var out []LinkTarget
	for rows.Next() {
		var t LinkTarget
		if err := rows.Scan(&t.EventID, &t.SourceEventID, &t.Title,
			&t.VenueName, &t.StartsAt, &t.Artists); err != nil {
			return nil, fmt.Errorf("store: scan link target: %w", err)
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// LinkEvents records that `eventID` is the same show as `canonicalID`.
//
// confirmed stays false: this is an automated guess, and the column exists so a
// human correction can be distinguished from a machine match later.
func (s *Store) LinkEvents(ctx context.Context, eventID, canonicalID string, confidence float64, method string) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO event_links (event_id, canonical_event_id, match_confidence, match_method)
		VALUES ($1, $2, $3, $4)
		ON CONFLICT (event_id) DO NOTHING`,
		eventID, canonicalID, confidence, method)
	if err != nil {
		return fmt.Errorf("store: link events: %w", err)
	}
	return nil
}

// SeedWatchlist is a development helper: it watchlists the most-saved events for
// a source so the availability loop has something to poll before the harness
// exists to promote events by hype score. Not part of the production path.
func (s *Store) SeedWatchlist(ctx context.Context, src string, limit int) (int, error) {
	tag, err := s.pool.Exec(ctx, `
		INSERT INTO watchlist (event_id, tier, watch_source, reason)
		SELECT id, 'hot', $1::source_platform, 'dev seed: top by attending'
		FROM events
		WHERE source = $1::source_platform
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
