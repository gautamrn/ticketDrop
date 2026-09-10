// Package stream publishes drop messages onto the Redis Stream that decouples
// ingestion from the agent harness. Go is the only writer to ticket:drops.
package stream

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"

	"ticketdrop/ingest/internal/source"
)

// Change kinds mirror the change_kind enum. "announced" comes from the
// discovery tier and means the show exists, not that tickets are buyable.
const (
	ChangeAnnounced      = "announced"
	ChangeNewTicketed    = "new_ticketed"
	ChangeTierAvailable  = "tier_available"
	ChangeReturnedToSale = "returned_to_sale"
)

type Publisher struct {
	rdb    *redis.Client
	stream string
	maxLen int64
}

func New(redisURL, stream string, maxLen int64) (*Publisher, error) {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, fmt.Errorf("stream: parse redis url: %w", err)
	}
	return &Publisher{rdb: redis.NewClient(opts), stream: stream, maxLen: maxLen}, nil
}

func (p *Publisher) Ping(ctx context.Context) error { return p.rdb.Ping(ctx).Err() }
func (p *Publisher) Close() error                   { return p.rdb.Close() }

type Drop struct {
	Change      string
	Event       source.Event
	Fingerprint string
	DetectedAt  time.Time
}

// Publish appends one drop and returns the generated drop_id, which is the
// idempotency key for the rest of the pipeline.
//
// MAXLEN with '~' trims approximately, letting Redis stop at a node boundary.
// Exact trimming would walk the radix tree on every append, adding latency to
// the one path we hold to a 50 ms budget.
func (p *Publisher) Publish(ctx context.Context, d Drop) (string, error) {
	dropID, err := uuid.NewV7()
	if err != nil {
		return "", fmt.Errorf("stream: generate drop id: %w", err)
	}

	ev := d.Event
	values := map[string]any{
		"drop_id":     dropID.String(),
		"source":      ev.Source,
		"event_id":    ev.SourceEventID,
		"event_url":   ev.ContentURL,
		"ticket_link": ev.TicketLink,
		"title":       ev.Title,
		"venue":       venueName(ev),
		"starts_at":   formatTime(ev.StartsAt),
		"artists":     artistNames(ev),
		"attending":   formatInt(ev.Attending),
		"queue_it":    strconv.FormatBool(ev.QueueIt),
		"change":      d.Change,
		"fingerprint": d.Fingerprint,
		"detected_at": d.DetectedAt.UTC().Format(time.RFC3339Nano),
		"raw":         string(ev.Raw),
	}

	if err := p.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: p.stream,
		MaxLen: p.maxLen,
		Approx: true,
		Values: values,
	}).Err(); err != nil {
		return "", fmt.Errorf("stream: xadd: %w", err)
	}
	return dropID.String(), nil
}

func venueName(ev source.Event) string {
	if ev.Venue == nil {
		return ""
	}
	return ev.Venue.Name
}

func artistNames(ev source.Event) string {
	names := make([]string, 0, len(ev.Artists))
	for _, a := range ev.Artists {
		names = append(names, a.Name)
	}
	return strings.Join(names, ", ")
}

func formatTime(t *time.Time) string {
	if t == nil {
		return ""
	}
	return t.UTC().Format(time.RFC3339)
}

func formatInt(n *int) string {
	if n == nil {
		return ""
	}
	return strconv.Itoa(*n)
}
