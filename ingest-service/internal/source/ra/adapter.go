package ra

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"ticketdrop/ingest/internal/source"
)

const (
	dateLayout = "2006-01-02"
	// RA returns LocalDateTime as an ISO string without a zone.
	localDateTimeLayout = "2006-01-02T15:04:05.000"
)

// Adapter implements the availability tier. It also enumerates events, which is
// the only way to obtain RA event ids for the watchlist.
type Adapter struct {
	client    *Client
	areaIDs   []string
	lookahead time.Duration
}

func NewAdapter(client *Client, areaIDs []string, lookahead time.Duration) *Adapter {
	return &Adapter{client: client, areaIDs: areaIDs, lookahead: lookahead}
}

func (a *Adapter) Name() string      { return source.PlatformRA }
func (a *Adapter) Tier() source.Tier { return source.TierAvailability }

// Enumerate lists events across the configured areas so their ids exist locally
// for the watchlist to reference. It emits no stream messages: appearing in a
// listing is not a sale-state change, and treating it as one would fire an alert
// for every event in a city on first run.
func (a *Adapter) Enumerate(ctx context.Context, pageSize int) ([]source.Event, error) {
	from := time.Now().UTC()
	to := from.Add(a.lookahead)

	var all []source.Event
	for _, area := range a.areaIDs {
		select {
		case <-ctx.Done():
			return all, ctx.Err()
		default:
		}

		events, _, err := a.client.Listings(ctx, area, from, to, pageSize, 1)
		if err != nil {
			return all, err
		}
		for _, e := range events {
			all = append(all, a.toEvent(e))
		}
	}
	return all, nil
}

// Snapshot reads the current sale state of one event.
func (a *Adapter) Snapshot(ctx context.Context, sourceEventID string) (source.Snapshot, error) {
	e, raw, err := a.client.Event(ctx, sourceEventID)
	if err != nil {
		return source.Snapshot{}, err
	}
	return source.Snapshot{
		SourceEventID:   e.ID,
		IsTicketed:      e.IsTicketed,
		Cost:            e.Cost,
		PresaleStatus:   e.PresaleStatus,
		ResaleActive:    e.ResaleActive,
		QueueIt:         e.QueueItEnabled,
		EventStatus:     e.Status,
		DateUpdated:     parseTime(e.DateUpdated),
		Attending:       e.Attending,
		InterestedCount: e.InterestedCount,
		ObservedAt:      time.Now().UTC(),
		Raw:             raw,
	}, nil
}

func (a *Adapter) toEvent(e APIEvent) source.Event {
	ev := source.Event{
		Source:        source.PlatformRA,
		SourceEventID: e.ID,
		Title:         strings.TrimSpace(e.Title),
		ContentURL:    absoluteURL(e.ContentURL),
		IsTicketed:    e.IsTicketed,
		QueueIt:       e.QueueItEnabled,
		Attending:     e.Attending,
		MinimumAge:    e.MinimumAge,
		Raw:           marshalOrNull(e),
	}

	if d := parseTime(e.Date); d != nil {
		ev.ListingDate = d
	}
	ev.StartsAt = firstNonNil(parseTime(e.StartTime), ev.ListingDate)
	ev.EndsAt = parseTime(e.EndTime)

	if e.Venue != nil && e.Venue.Name != "" {
		v := &source.Venue{
			SourceVenueID: e.Venue.ID,
			Name:          e.Venue.Name,
			ContentURL:    absoluteURL(e.Venue.ContentURL),
		}
		if e.Venue.Area != nil {
			v.AreaName = e.Venue.Area.Name
		}
		ev.Venue = v
	}

	for _, ar := range e.Artists {
		if name := strings.TrimSpace(ar.Name); name != "" {
			ev.Artists = append(ev.Artists, source.Artist{
				SourceArtistID: ar.ID,
				Name:           name,
				ContentURL:     absoluteURL(ar.ContentURL),
			})
		}
	}
	for _, g := range e.Genres {
		if n := strings.TrimSpace(g.Name); n != "" {
			ev.Genres = append(ev.Genres, n)
		}
	}
	return ev
}

// absoluteURL expands the site-relative paths RA returns in contentUrl, so
// stored links are usable without knowing where they came from.
func absoluteURL(path string) string {
	p := strings.TrimSpace(path)
	switch {
	case p == "":
		return ""
	case strings.HasPrefix(p, "http://"), strings.HasPrefix(p, "https://"):
		return p
	case strings.HasPrefix(p, "/"):
		return "https://ra.co" + p
	default:
		return "https://ra.co/" + p
	}
}

func parseTime(s string) *time.Time {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil
	}
	for _, layout := range []string{
		time.RFC3339Nano, time.RFC3339, localDateTimeLayout,
		"2006-01-02T15:04:05", dateLayout,
	} {
		if t, err := time.Parse(layout, s); err == nil {
			t = t.UTC()
			return &t
		}
	}
	return nil
}

func firstNonNil(vals ...*time.Time) *time.Time {
	for _, v := range vals {
		if v != nil {
			return v
		}
	}
	return nil
}

func marshalOrNull(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		return json.RawMessage("null")
	}
	return b
}

// ParseAreaIDs accepts the numeric area codes RA uses in its event URLs.
func ParseAreaIDs(vals []string) []string {
	out := make([]string, 0, len(vals))
	for _, v := range vals {
		v = strings.TrimSpace(v)
		if v == "" {
			continue
		}
		if _, err := strconv.Atoi(v); err != nil {
			continue
		}
		out = append(out, v)
	}
	return out
}
