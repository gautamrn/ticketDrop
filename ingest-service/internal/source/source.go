// Package source defines the normalized shape every ticketing source is
// mapped into, plus the adapter contract. Vendor quirks stay inside an
// adapter; nothing downstream branches on which platform an event came from.
package source

import (
	"context"
	"encoding/json"
	"time"
)

// Tier separates the two ingestion cadences described in ARCHITECTURE.md §1.1.
// An adapter belongs to exactly one.
type Tier string

const (
	// TierDiscovery answers "what shows exist", on a slow loop.
	TierDiscovery Tier = "discovery"
	// TierAvailability answers "did the sale state change", on a fast loop
	// over watchlisted events only.
	TierAvailability Tier = "availability"
)

// Platform values match the source_platform enum in the database.
const (
	PlatformRA       = "ra"
	PlatformEDMTrain = "edmtrain"
)

type Venue struct {
	SourceVenueID string
	Name          string
	AreaName      string
	Latitude      *float64
	Longitude     *float64
	ContentURL    string
}

type Artist struct {
	SourceArtistID string
	Name           string
	ContentURL     string
	// B2BWithNext means this artist performs back to back with the next
	// artist in the lineup. A b2b set is a stronger signal than either name
	// alone, so hype scoring reads it rather than flattening the lineup.
	B2BWithNext bool
}

// Event is the normalized event. Pointer fields are genuinely optional: a nil
// StartsAt means the source did not tell us, which is different from midnight.
type Event struct {
	Source        string
	SourceEventID string
	Title         string
	ContentURL    string
	TicketLink    string

	AnnouncedAt *time.Time
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

// DiscoveryAdapter finds shows that exist. Discover must be idempotent: it may
// be called with an overlapping window and is expected to return the same
// events again. Deduplication is the caller's job, enforced by a unique
// constraint rather than by adapter bookkeeping.
type DiscoveryAdapter interface {
	Name() string
	Tier() Tier
	Discover(ctx context.Context, since time.Time) ([]Event, error)
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
	Tier() Tier
	Snapshot(ctx context.Context, sourceEventID string) (Snapshot, error)
}
