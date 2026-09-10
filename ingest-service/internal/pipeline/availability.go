package pipeline

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"ticketdrop/ingest/internal/cache"
	"ticketdrop/ingest/internal/fingerprint"
	"ticketdrop/ingest/internal/metrics"
	"ticketdrop/ingest/internal/pool"
	"ticketdrop/ingest/internal/source"
	"ticketdrop/ingest/internal/source/ra"
	"ticketdrop/ingest/internal/store"
	"ticketdrop/ingest/internal/stream"
)

// Availability polls watchlisted events on a fast cadence, diffs each result
// against the last observed snapshot, and publishes only classified changes.
type Availability struct {
	adapter   source.AvailabilityAdapter
	store     *store.Store
	cache     *cache.Store
	publisher *stream.Publisher
	pool      *pool.Pool
	log       *slog.Logger

	interval  time.Duration
	batchSize int
}

func NewAvailability(
	adapter source.AvailabilityAdapter,
	st *store.Store,
	c *cache.Store,
	pub *stream.Publisher,
	p *pool.Pool,
	log *slog.Logger,
	interval time.Duration,
	batchSize int,
) *Availability {
	return &Availability{
		adapter:   adapter,
		store:     st,
		cache:     c,
		publisher: pub,
		pool:      p,
		log:       log.With("source", adapter.Name(), "tier", string(adapter.Tier())),
		interval:  interval,
		batchSize: batchSize,
	}
}

func (a *Availability) Run(ctx context.Context) error {
	a.log.Info("availability loop starting",
		"interval", a.interval.String(), "batch", a.batchSize)

	a.pool.Start(ctx)
	defer a.pool.Close()

	ticker := time.NewTicker(a.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			a.log.Info("availability loop stopped")
			return nil
		case <-ticker.C:
			a.dispatch(ctx)
		}
	}
}

// dispatch enqueues one round of work and returns immediately.
//
// It does not wait for the round to finish. Waiting would couple the tick rate
// to the slowest event in the batch, so a single hung request would stall every
// other event's cadence. Bounding concurrency is the pool's job; bounding
// latency is the per-request timeout's job.
func (a *Availability) dispatch(ctx context.Context) {
	items, err := a.store.DueWatchItems(ctx, a.adapter.Name(), a.batchSize)
	if err != nil {
		a.log.Error("watchlist query failed", "err", err)
		return
	}
	metrics.WatchlistDue.WithLabelValues(a.adapter.Name()).Set(float64(len(items)))
	metrics.PoolQueueDepth.WithLabelValues(a.adapter.Name()).Set(float64(a.pool.Queued()))

	for _, item := range items {
		it := item
		if !a.pool.TrySubmit(func(c context.Context) { a.check(c, it) }) {
			// Queue full. Dropping is correct: this job is already stale, and
			// the next tick produces a fresher one for the same event. The
			// counter is the signal that workers or upstream latency need
			// attention.
			metrics.PollDropped.WithLabelValues(a.adapter.Name()).Inc()
		}
	}
}

func (a *Availability) check(ctx context.Context, item store.WatchItem) {
	name := a.adapter.Name()
	started := time.Now()

	snap, err := a.adapter.Snapshot(ctx, item.SourceEventID)
	metrics.PollDuration.WithLabelValues(name).Observe(time.Since(started).Seconds())
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return
		}
		metrics.PollTotal.WithLabelValues(name, raStatus(err)).Inc()
		a.log.Warn("snapshot failed", "event", item.SourceEventID, "err", err)
		return
	}
	metrics.PollTotal.WithLabelValues(name, "ok").Inc()

	// Mark polled regardless of outcome so a permanently broken event cannot
	// hold the head of the least-recently-polled queue forever.
	if err := a.store.MarkPolled(ctx, item.EventID); err != nil {
		a.log.Warn("mark polled failed", "event", item.SourceEventID, "err", err)
	}

	fp := fingerprint.Compute(snap)
	prev, hadPrev, err := a.cache.Previous(ctx, name, item.SourceEventID)
	if err != nil {
		a.log.Warn("prior snapshot lookup failed", "event", item.SourceEventID, "err", err)
		return
	}

	if !hadPrev {
		// First sighting. Record the baseline and emit nothing: without a prior
		// state every watchlisted event would look like a fresh drop, which is
		// the cold-start stampede described in ARCHITECTURE.md §9.
		if err := a.cache.Save(ctx, name, snap); err != nil {
			a.log.Warn("baseline save failed", "event", item.SourceEventID, "err", err)
		}
		return
	}

	if fingerprint.Compute(prev) == fp {
		return // nothing changed, the overwhelmingly common case
	}

	// Persist the transition before deciding whether it is alertable, so the
	// history records what we saw even when we choose not to act on it.
	if err := a.store.RecordSnapshot(ctx, item.EventID, fp, snap); err != nil {
		a.log.Warn("record snapshot failed", "event", item.SourceEventID, "err", err)
	}
	if err := a.cache.Save(ctx, name, snap); err != nil {
		a.log.Warn("snapshot save failed", "event", item.SourceEventID, "err", err)
		return
	}

	change, ok := fingerprint.Classify(prev, snap)
	if !ok {
		// The fingerprint moved but no rule explains it: a flyer swap, a
		// description edit, a dateUpdated bump. Recorded above, not alerted.
		metrics.UnclassifiedChange.WithLabelValues(name).Inc()
		a.log.Debug("unclassified change", "event", item.SourceEventID)
		return
	}

	if err := a.publish(ctx, item, snap, fp, change); err != nil {
		a.log.Error("publish failed", "event", item.SourceEventID, "err", err)
		return
	}
	a.log.Info("drop detected",
		"event", item.SourceEventID, "title", item.Title, "change", change,
		"took", time.Since(started).Round(time.Millisecond).String())
}

func (a *Availability) publish(
	ctx context.Context,
	item store.WatchItem,
	snap source.Snapshot,
	fp, change string,
) error {
	// Merge the durable catalogue fields with the freshly observed state. The
	// snapshot is authoritative for anything that can change mid-sale.
	ev := source.Event{
		Source:        item.Source,
		SourceEventID: item.SourceEventID,
		Title:         item.Title,
		ContentURL:    item.ContentURL,
		TicketLink:    item.TicketLink,
		StartsAt:      item.StartsAt,
		IsTicketed:    snap.IsTicketed,
		QueueIt:       snap.QueueIt,
		Attending:     snap.Attending,
		Raw:           snap.Raw,
	}
	if item.VenueName != "" {
		ev.Venue = &source.Venue{Name: item.VenueName}
	}
	for _, name := range item.Artists {
		ev.Artists = append(ev.Artists, source.Artist{Name: name})
	}

	started := time.Now()
	_, err := a.publisher.Publish(ctx, stream.Drop{
		Change:      change,
		Event:       ev,
		Fingerprint: fp,
		DetectedAt:  snap.ObservedAt,
	})
	metrics.PublishDuration.WithLabelValues(item.Source).Observe(time.Since(started).Seconds())
	metrics.PublishTotal.WithLabelValues(item.Source, change, statusOf(err)).Inc()
	return err
}

func raStatus(err error) string {
	switch {
	case errors.Is(err, ra.ErrRateLimited):
		return "rate_limited"
	case errors.Is(err, ra.ErrBlocked):
		return "blocked"
	case errors.Is(err, ra.ErrNotFound):
		return "not_found"
	default:
		return "error"
	}
}

// Enumerator periodically lists events in the configured areas so their ids
// exist locally. Slow loop, no stream output.
type Enumerator struct {
	adapter  *ra.Adapter
	store    *store.Store
	log      *slog.Logger
	interval time.Duration
	pageSize int
}

func NewEnumerator(adapter *ra.Adapter, st *store.Store, log *slog.Logger,
	interval time.Duration, pageSize int) *Enumerator {
	return &Enumerator{
		adapter: adapter, store: st,
		log:      log.With("source", adapter.Name(), "loop", "enumerate"),
		interval: interval, pageSize: pageSize,
	}
}

func (e *Enumerator) Run(ctx context.Context) error {
	e.log.Info("enumerate loop starting", "interval", e.interval.String())

	e.once(ctx)

	ticker := time.NewTicker(e.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			e.once(ctx)
		}
	}
}

func (e *Enumerator) once(ctx context.Context) {
	started := time.Now()
	events, err := e.adapter.Enumerate(ctx, e.pageSize)
	if err != nil && !errors.Is(err, context.Canceled) {
		e.log.Error("enumerate failed", "err", err, "partial", len(events))
	}

	var stored, fresh int
	for _, ev := range events {
		res, err := e.store.UpsertEvent(ctx, ev)
		if err != nil {
			e.log.Warn("upsert failed", "event", ev.SourceEventID, "err", err)
			continue
		}
		stored++
		if res.IsNew {
			fresh++
		}
	}
	metrics.EventsSeen.WithLabelValues(e.adapter.Name()).Add(float64(len(events)))
	metrics.EventsNew.WithLabelValues(e.adapter.Name()).Add(float64(fresh))

	e.log.Info("enumerate complete",
		"seen", len(events), "stored", stored, "new", fresh,
		"took", time.Since(started).Round(time.Millisecond).String())
}
