// Package cache holds the Redis-backed prior-state store used by the
// availability tier.
package cache

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"

	"ticketdrop/ingest/internal/source"
)

// snapshotTTL matches the 30-day retention in ARCHITECTURE.md §5. An event we
// have not seen in a month is almost certainly past, and letting keys expire
// keeps Redis bounded without a sweeper.
const snapshotTTL = 30 * 24 * time.Hour

// Store keeps the last observed snapshot per event.
//
// It stores the whole snapshot rather than just its fingerprint. A hash alone
// answers "did something change" but not "what changed", and classification
// needs the previous field values to distinguish a presale opening from a flyer
// swap. A snapshot is a couple of hundred bytes, so the extra memory buys the
// only information that makes the delta actionable.
type Store struct {
	rdb *redis.Client
}

func New(redisURL string) (*Store, error) {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, fmt.Errorf("cache: parse redis url: %w", err)
	}
	return &Store{rdb: redis.NewClient(opts)}, nil
}

func (s *Store) Close() error { return s.rdb.Close() }

func key(src, sourceEventID string) string {
	return "snap:" + src + ":" + sourceEventID
}

// Previous returns the last stored snapshot. found == false means we have never
// seen this event, which is not an error and must not be treated as a change.
func (s *Store) Previous(ctx context.Context, src, sourceEventID string) (source.Snapshot, bool, error) {
	raw, err := s.rdb.Get(ctx, key(src, sourceEventID)).Bytes()
	if errors.Is(err, redis.Nil) {
		return source.Snapshot{}, false, nil
	}
	if err != nil {
		return source.Snapshot{}, false, fmt.Errorf("cache: get snapshot: %w", err)
	}

	var snap source.Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		// A snapshot we cannot decode is treated as absent rather than fatal:
		// the shape may have changed across a deploy, and the cost is one
		// suppressed comparison rather than a crash-looping poller.
		return source.Snapshot{}, false, nil
	}
	return snap, true, nil
}

func (s *Store) Save(ctx context.Context, src string, snap source.Snapshot) error {
	// Raw payloads are large and only needed for durable history in Postgres,
	// so they do not go into the hot cache.
	snap.Raw = nil

	buf, err := json.Marshal(snap)
	if err != nil {
		return fmt.Errorf("cache: marshal snapshot: %w", err)
	}
	if err := s.rdb.Set(ctx, key(src, snap.SourceEventID), buf, snapshotTTL).Err(); err != nil {
		return fmt.Errorf("cache: save snapshot: %w", err)
	}
	return nil
}
