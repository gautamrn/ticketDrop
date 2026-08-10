// Package pipeline runs the ingestion loops.
package pipeline

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"ticketdrop/ingest/internal/metrics"
	"ticketdrop/ingest/internal/source"
	"ticketdrop/ingest/internal/source/edmtrain"
	"ticketdrop/ingest/internal/store"
	"ticketdrop/ingest/internal/stream"
)

// Discovery polls a discovery-tier adapter on a fixed cadence and publishes
// an "announced" message for every event it has not seen before.
//
// Deliberately sequential: one adapter, one in-flight request, no fan-out.
// EDMTrain's terms set rate limits at their discretion and forbid working
// around them, and the discovery tier has no latency requirement that would
// justify concurrency. The bounded worker pool belongs to the availability
// tier, where hundreds of watchlisted events share a 2 s deadline.
type Discovery struct {
	adapter   source.DiscoveryAdapter
	store     *store.Store
	publisher *stream.Publisher
	log       *slog.Logger

	interval time.Duration
	lookback time.Duration
}

func NewDiscovery(
	adapter source.DiscoveryAdapter,
	st *store.Store,
	pub *stream.Publisher,
	log *slog.Logger,
	interval, lookback time.Duration,
) *Discovery {
	return &Discovery{
		adapter:   adapter,
		store:     st,
		publisher: pub,
		log:       log.With("source", adapter.Name(), "tier", string(adapter.Tier())),
		interval:  interval,
		lookback:  lookback,
	}
}

func (d *Discovery) Run(ctx context.Context) error {
	d.log.Info("discovery loop starting",
		"interval", d.interval.String(), "lookback", d.lookback.String())

	ticker := time.NewTicker(d.interval)
	defer ticker.Stop()

	if err := d.pollOnce(ctx); err != nil && !errors.Is(err, context.Canceled) {
		d.log.Error("initial poll failed", "err", err)
		if fatal(err) {
			return err
		}
	}

	for {
		select {
		case <-ctx.Done():
			d.log.Info("discovery loop stopped")
			return nil
		case <-ticker.C:
			if err := d.pollOnce(ctx); err != nil {
				if errors.Is(err, context.Canceled) {
					continue
				}
				d.log.Error("poll failed", "err", err)
				// A revoked or invalid key cannot be retried into working, and
				// hammering it is exactly what their terms prohibit. Stop and
				// let the operator intervene.
				if fatal(err) {
					return err
				}
			}
		}
	}
}

// fatal separates faults a human must fix from faults that resolve themselves.
func fatal(err error) bool { return errors.Is(err, edmtrain.ErrUnauthorized) }

func (d *Discovery) pollOnce(ctx context.Context) error {
	name := d.adapter.Name()
	started := time.Now()

	since, err := d.since(ctx, name, started)
	if err != nil {
		return err
	}

	events, err := d.adapter.Discover(ctx, since)
	metrics.PollDuration.WithLabelValues(name).Observe(time.Since(started).Seconds())
	if err != nil {
		metrics.PollTotal.WithLabelValues(name, status(err)).Inc()
		return err
	}
	metrics.PollTotal.WithLabelValues(name, "ok").Inc()
	metrics.EventsSeen.WithLabelValues(name).Add(float64(len(events)))

	var announced int
	for _, ev := range events {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		res, err := d.store.UpsertEvent(ctx, ev)
		if err != nil {
			// One malformed event must not abort the batch; the rest of the
			// poll is still useful and the next cycle will retry this one.
			d.log.Warn("upsert failed", "event_id", ev.SourceEventID, "err", err)
			continue
		}
		if !res.IsNew {
			continue
		}
		metrics.EventsNew.WithLabelValues(name).Inc()

		if err := d.announce(ctx, ev); err != nil {
			d.log.Error("publish failed", "event_id", ev.SourceEventID, "err", err)
			continue
		}
		announced++
	}

	// Advance only after a fully successful poll. Failing before this point
	// leaves the cursor where it was, so the next attempt re-covers the gap.
	if err := d.store.SetWatermark(ctx, name, started); err != nil {
		return err
	}
	metrics.WatermarkAge.WithLabelValues(name).Set(0)

	d.log.Info("poll complete",
		"seen", len(events),
		"announced", announced,
		"took", time.Since(started).Round(time.Millisecond).String())
	return nil
}

// since computes the query floor: the stored cursor pulled back by the safety
// overlap. Re-fetching is free because dedupe is a unique constraint, so the
// window errs wide rather than risking a missed event between polls.
func (d *Discovery) since(ctx context.Context, name string, now time.Time) (time.Time, error) {
	mark, ok, err := d.store.Watermark(ctx, name)
	if err != nil {
		return time.Time{}, err
	}
	if !ok {
		// Cold start: no cursor. Reach back one lookback window rather than
		// the beginning of time.
		return now.Add(-d.lookback), nil
	}
	metrics.WatermarkAge.WithLabelValues(name).Set(now.Sub(mark).Seconds())

	floor := mark.Add(-d.lookback)
	if oldest := now.Add(-30 * 24 * time.Hour); floor.Before(oldest) {
		// A long outage should not trigger a month-wide backfill in one poll.
		return oldest, nil
	}
	return floor, nil
}

func (d *Discovery) announce(ctx context.Context, ev source.Event) error {
	started := time.Now()
	_, err := d.publisher.Publish(ctx, stream.Drop{
		Change: stream.ChangeAnnounced,
		Event:  ev,
		// An announcement has no availability state to fingerprint. The empty
		// value is meaningful: it marks a discovery-tier message.
		Fingerprint: "",
		DetectedAt:  started,
	})
	metrics.PublishDuration.WithLabelValues(ev.Source).Observe(time.Since(started).Seconds())
	metrics.PublishTotal.WithLabelValues(ev.Source, stream.ChangeAnnounced, statusOf(err)).Inc()
	return err
}

func status(err error) string {
	switch {
	case errors.Is(err, edmtrain.ErrRateLimited):
		return "rate_limited"
	case errors.Is(err, edmtrain.ErrUnauthorized):
		return "unauthorized"
	default:
		return "error"
	}
}

func statusOf(err error) string {
	if err != nil {
		return "error"
	}
	return "ok"
}
