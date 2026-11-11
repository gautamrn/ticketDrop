// Package source defines the normalized Resident Advisor catalogue and
// availability shapes. RA quirks stay inside its adapter.
package source

import (
	"context"
	"encoding/json"
	"time"
)

const PlatformRA = "ra"

type Venue struct {
	SourceVenueID string
	Name          string
	AreaID        *int
	AreaName      string
	Latitude      *float64
	Longitude     *float64
	ContentURL    string
}

type Artist struct {
	SourceArtistID string
	Name           string
	ContentURL     string
}

// Event is the normalized event. Pointer fields are genuinely optional: a nil
// StartsAt means the source did not tell us, which is different from midnight.
type Event struct {
	Source        string
	SourceEventID string
	Title         string
	ContentURL    string
	TicketLink    string

	StartsAt    *time.Time
	EndsAt      *time.Time
	ListingDate *time.Time

	IsTicketed bool
	QueueIt    bool
	Attending  *int
	MinimumAge *int
	Genres     []string

	Venue   *Venue
	Artists []Artist

	Raw json.RawMessage
}

// Snapshot is everything we can observe about one event's sale state at one
// instant.
//
// The field set is deliberately narrow: it is exactly what RA returns to an
// unauthenticated client. Per-tier data (name, price, sold-out state) is not
// here because RA's Event.tickets returns an empty array without a promoter
// session — verified against the live schema, see ARCHITECTURE.md §1.2. Adding
// a field here that we cannot actually read would produce a fingerprint that
// never changes and a detector that never fires.
type Snapshot struct {
	SourceEventID string

	IsTicketed    bool
	Cost          string
	PresaleStatus string
	ResaleActive  bool
	QueueIt       bool
	EventStatus   string

	DateUpdated     *time.Time
	Attending       *int
	InterestedCount *int

	ObservedAt time.Time
	Raw        json.RawMessage
}

// AvailabilityAdapter reports the current sale state of a single known event.
// One call per event per poll, which is why the caller runs it behind a bounded
// worker pool rather than a goroutine per event.
type AvailabilityAdapter interface {
	Name() string
	Snapshot(ctx context.Context, sourceEventID string) (Snapshot, error)
}
