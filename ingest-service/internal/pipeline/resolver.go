package pipeline

import (
	"context"
	"log/slog"
	"time"

	"ticketdrop/ingest/internal/metrics"
	"ticketdrop/ingest/internal/resolve"
	"ticketdrop/ingest/internal/source"
	"ticketdrop/ingest/internal/store"
)

// Resolver links discovery-tier events to their availability-tier counterparts.
//
// Without this the two tiers are separate systems that happen to share a
// database: EDMTrain knows a show was announced, RA knows whether its tickets
// moved, and nothing connects the two. The link is what lets a show discovered
// on one source be watched on the other.
//
// It runs slowly and off the hot path. A missed link costs one unwatched show;
// a wrong link costs the user's trust in every alert, so the matcher is
// deliberately conservative and this loop never retries a rejection differently.
type Resolver struct {
	store *store.Store
	log   *slog.Logger

	from, to string
	interval time.Duration
	batch    int
}

func NewResolver(st *store.Store, log *slog.Logger, from, to string,
	interval time.Duration, batch int) *Resolver {
	return &Resolver{
		store: st,
		log:   log.With("loop", "resolve", "from", from, "to", to),
		from:  from, to: to, interval: interval, batch: batch,
	}
}

func (r *Resolver) Run(ctx context.Context) error {
	r.log.Info("resolver loop starting", "interval", r.interval.String())

	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			r.once(ctx)
		}
	}
}

func (r *Resolver) once(ctx context.Context) {
	pending, err := r.store.UnlinkedEvents(ctx, r.from, r.batch)
	if err != nil {
		r.log.Error("unlinked query failed", "err", err)
		return
	}
	if len(pending) == 0 {
		return
	}

	var linked, rejected int
	for _, ev := range pending {
		candidates, err := r.store.LinkCandidates(ctx, r.to, ev.StartsAt, resolve.DateSlack)
		if err != nil {
			r.log.Warn("candidate query failed", "event", ev.SourceEventID, "err", err)
			continue
		}
		if len(candidates) == 0 {
			continue
		}

		match, ok := resolve.Best(resolve.Query{
			Title:     ev.Title,
			VenueName: ev.VenueName,
			Date:      ev.StartsAt,
			Artists:   ev.Artists,
		}, toCandidates(candidates))

		if !ok {
			// Left unlinked on purpose. The next pass will reconsider it, which
			// matters because enumerate may not have stored the RA counterpart
			// yet — absence of a candidate now is not evidence there is none.
			rejected++
			r.log.Debug("no confident match",
				"event", ev.SourceEventID, "title", ev.Title,
				"best", match.Score, "runner_up", match.RunnerUp)
			continue
		}

		if err := r.store.LinkEvents(ctx, ev.EventID, match.Candidate.SourceEventID,
			match.Score, "fuzzy:venue+lineup+date"); err != nil {
			r.log.Warn("link failed", "event", ev.SourceEventID, "err", err)
			continue
		}
		linked++
		r.log.Info("linked across sources",
			"from_event", ev.SourceEventID, "to_event", match.Candidate.SourceEventID,
			"title", ev.Title, "score", match.Score)
	}

	metrics.LinksCreated.WithLabelValues(r.from, r.to).Add(float64(linked))
	metrics.LinksRejected.WithLabelValues(r.from, r.to).Add(float64(rejected))
	r.log.Info("resolve pass complete",
		"pending", len(pending), "linked", linked, "rejected", rejected)
}

// toCandidates carries the internal event id through as the candidate id so the
// link can be written without a second lookup.
func toCandidates(targets []store.LinkTarget) []resolve.Candidate {
	out := make([]resolve.Candidate, 0, len(targets))
	for _, t := range targets {
		out = append(out, resolve.Candidate{
			SourceEventID: t.EventID,
			Title:         t.Title,
			VenueName:     t.VenueName,
			Date:          t.StartsAt,
			Artists:       t.Artists,
		})
	}
	return out
}

// Compile-time assertion that the platform constants used for linking are the
// ones the rest of the service knows about.
var _, _ = source.PlatformEDMTrain, source.PlatformRA
