package edmtrain

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"ticketdrop/ingest/internal/source"
)

const (
	dateLayout     = "2006-01-02"
	dateTimeLayout = "2006-01-02 15:04:05"
)

type Adapter struct {
	client             *Client
	locationIDs        []string
	includeLivestreams bool
}

func NewAdapter(client *Client, locationIDs []string, includeLivestreams bool) *Adapter {
	return &Adapter{
		client:             client,
		locationIDs:        locationIDs,
		includeLivestreams: includeLivestreams,
	}
}

func (a *Adapter) Name() string      { return source.PlatformEDMTrain }
func (a *Adapter) Tier() source.Tier { return source.TierDiscovery }

// Discover returns every event EDMTrain recorded at or after `since`. The
// window is intentionally re-fetched with overlap on each poll; callers dedupe
// on a unique constraint, so returning the same event twice is not an error.
func (a *Adapter) Discover(ctx context.Context, since time.Time) ([]source.Event, error) {
	raw, err := a.client.Events(ctx, EventQuery{
		LocationIDs:        a.locationIDs,
		CreatedStartDate:   since,
		IncludeLivestreams: a.includeLivestreams,
	})
	if err != nil {
		return nil, err
	}

	out := make([]source.Event, 0, len(raw))
	for _, e := range raw {
		if !a.include(e) {
			continue
		}
		out = append(out, a.toEvent(e))
	}
	return out, nil
}

func (a *Adapter) include(e APIEvent) bool {
	if e.LivestreamInd && !a.includeLivestreams {
		return false
	}
	// The API defaults to electronic-only, but the flag is authoritative and
	// a location filter can pull in other-genre shows at a shared venue.
	return e.ElectronicGenreInd
}

func (a *Adapter) toEvent(e APIEvent) source.Event {
	ev := source.Event{
		Source:        source.PlatformEDMTrain,
		SourceEventID: strconv.FormatInt(e.ID, 10),
		Title:         title(e),
		ContentURL:    e.Link,
		TicketLink:    e.TicketLink,
		// EDMTrain lists shows; it has no concept of ticket inventory. A
		// ticketLink means "somewhere to buy", not "on sale now", which is
		// why discovery never triggers reservation.
		IsTicketed:  e.TicketLink != "",
		AnnouncedAt: parseTime(e.CreatedDate),
		MinimumAge:  parseAges(e.Ages),
		Raw:         rawOrNull(e.raw),
	}

	if d := parseTime(e.Date); d != nil {
		ev.ListingDate = d
		ev.StartsAt = combine(*d, e.StartTime)
	}
	if ev.StartsAt != nil {
		ev.EndsAt = combine(*ev.StartsAt, e.EndTime)
		// A show ending "before" it starts crossed midnight.
		if ev.EndsAt != nil && ev.EndsAt.Before(*ev.StartsAt) {
			t := ev.EndsAt.Add(24 * time.Hour)
			ev.EndsAt = &t
		}
	}

	if e.Venue.Name != "" {
		ev.Venue = &source.Venue{
			SourceVenueID: strconv.FormatInt(e.Venue.ID, 10),
			Name:          e.Venue.Name,
			AreaName:      areaName(e.Venue),
			Latitude:      e.Venue.Latitude,
			Longitude:     e.Venue.Longitude,
		}
	}

	for _, ar := range e.ArtistList {
		if strings.TrimSpace(ar.Name) == "" {
			continue
		}
		ev.Artists = append(ev.Artists, source.Artist{
			SourceArtistID: strconv.FormatInt(ar.ID, 10),
			Name:           strings.TrimSpace(ar.Name),
			ContentURL:     ar.Link,
			B2BWithNext:    ar.B2BInd,
		})
	}

	if e.FestivalInd {
		ev.Genres = append(ev.Genres, "festival")
	}
	return ev
}

// title falls back to the lineup because most EDMTrain events have no name —
// per their docs, an event only carries a name when it has no artists.
func title(e APIEvent) string {
	if n := strings.TrimSpace(e.Name); n != "" {
		return n
	}
	names := make([]string, 0, len(e.ArtistList))
	for _, a := range e.ArtistList {
		if n := strings.TrimSpace(a.Name); n != "" {
			names = append(names, n)
		}
	}
	if len(names) == 0 {
		return fmt.Sprintf("EDMTrain event %d", e.ID)
	}
	if len(names) > 4 {
		return strings.Join(names[:4], ", ") + " + more"
	}
	return strings.Join(names, ", ")
}

func areaName(v APIVenue) string {
	switch {
	case v.Location != "" && v.State != "":
		return v.Location + ", " + v.State
	case v.Location != "":
		return v.Location
	default:
		return v.State
	}
}

// parseTime accepts the documented date form plus the timestamp forms the API
// has been observed to use, because createdDate granularity is not guaranteed.
func parseTime(s string) *time.Time {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil
	}
	for _, layout := range []string{time.RFC3339, dateTimeLayout, dateLayout} {
		if t, err := time.Parse(layout, s); err == nil {
			t = t.UTC()
			return &t
		}
	}
	return nil
}

// combine folds a "HH:MM:SS" local time onto a date. EDMTrain only supplies
// times for livestreams, so this is usually a no-op.
func combine(day time.Time, clock string) *time.Time {
	clock = strings.TrimSpace(clock)
	if clock == "" {
		return nil
	}
	for _, layout := range []string{"15:04:05", "15:04"} {
		if t, err := time.Parse(layout, clock); err == nil {
			out := time.Date(day.Year(), day.Month(), day.Day(),
				t.Hour(), t.Minute(), t.Second(), 0, time.UTC)
			return &out
		}
	}
	return nil
}

// parseAges pulls a minimum age out of free text like "21+" or "18 and over".
func parseAges(s string) *int {
	digits := strings.Builder{}
	for _, r := range s {
		if r >= '0' && r <= '9' {
			digits.WriteRune(r)
			continue
		}
		if digits.Len() > 0 {
			break
		}
	}
	if digits.Len() == 0 {
		return nil
	}
	n, err := strconv.Atoi(digits.String())
	if err != nil || n <= 0 || n > 99 {
		return nil
	}
	return &n
}

func rawOrNull(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("null")
	}
	return raw
}
