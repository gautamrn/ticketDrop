package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"ticketdrop/ingest/internal/metrics"
	"ticketdrop/ingest/internal/store"
	"ticketdrop/ingest/internal/stream"
)

const (
	outboxBatchSize = 100
	outboxLease     = 30 * time.Second
	outboxIdle      = 250 * time.Millisecond
)

type outboxStore interface {
	ClaimOutbox(context.Context, int, time.Duration) ([]store.OutboxItem, error)
	MarkOutboxPublished(context.Context, string) error
	RetryOutbox(context.Context, string, string, time.Duration) error
}

type dropPublisher interface {
	Publish(context.Context, stream.Drop) (string, error)
}

// OutboxDispatcher moves durably committed drops to Redis. A crash after
// Publish but before MarkOutboxPublished repeats the same drop ID, which the
// consumer's Postgres constraints and provider idempotency key absorb.
type OutboxDispatcher struct {
	store     outboxStore
	publisher dropPublisher
	log       *slog.Logger
}

func NewOutboxDispatcher(
	st outboxStore,
	publisher dropPublisher,
	log *slog.Logger,
) *OutboxDispatcher {
	return &OutboxDispatcher{
		store:     st,
		publisher: publisher,
		log:       log.With("loop", "outbox"),
	}
}

func (d *OutboxDispatcher) Run(ctx context.Context) error {
	d.log.Info("outbox dispatcher starting")
	for {
		processed, err := d.dispatch(ctx)
		if err != nil {
			d.log.Error("outbox dispatch failed", "err", err)
		}
		if processed > 0 {
			continue
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(outboxIdle):
		}
	}
}

func (d *OutboxDispatcher) dispatch(ctx context.Context) (int, error) {
	items, err := d.store.ClaimOutbox(ctx, outboxBatchSize, outboxLease)
	if err != nil {
		return 0, err
	}
	for _, item := range items {
		if err := d.publish(ctx, item); err != nil {
			d.log.Error("outbox item failed", "drop_id", item.ID, "err", err)
		}
	}
	return len(items), nil
}

func (d *OutboxDispatcher) publish(ctx context.Context, item store.OutboxItem) error {
	var drop stream.Drop
	if err := json.Unmarshal(item.Payload, &drop); err != nil {
		return d.retry(ctx, item, fmt.Errorf("decode payload: %w", err))
	}
	if drop.ID != item.ID {
		return d.retry(ctx, item, fmt.Errorf("payload id %q does not match row", drop.ID))
	}

	started := time.Now()
	_, err := d.publisher.Publish(ctx, drop)
	metrics.PublishDuration.WithLabelValues(drop.Event.Source).Observe(time.Since(started).Seconds())
	metrics.PublishTotal.WithLabelValues(drop.Event.Source, drop.Change, statusOf(err)).Inc()
	if err != nil {
		return d.retry(ctx, item, err)
	}
	if err := d.store.MarkOutboxPublished(ctx, item.ID); err != nil {
		// Leave the lease in place. Once it expires, the stable ID is
		// republished and consumer-side idempotency absorbs the duplicate.
		return err
	}
	return nil
}

func (d *OutboxDispatcher) retry(
	ctx context.Context,
	item store.OutboxItem,
	cause error,
) error {
	delay := outboxBackoff(item.Attempts)
	if err := d.store.RetryOutbox(ctx, item.ID, cause.Error(), delay); err != nil {
		return fmt.Errorf("%v; schedule retry: %w", cause, err)
	}
	return cause
}

func outboxBackoff(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	if attempt > 6 {
		attempt = 6
	}
	return time.Second * time.Duration(1<<(attempt-1))
}
