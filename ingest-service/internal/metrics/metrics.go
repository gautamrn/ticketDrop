// Package metrics holds the Prometheus surface for the ingest service.
//
// Every stage is instrumented because the latency budget in ARCHITECTURE.md §3
// is only enforceable if it is measured. A stage that cannot be measured
// cannot be optimized.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	PollTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_poll_total",
		Help: "Source polls attempted, by outcome.",
	}, []string{"source", "status"})

	PollDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "ingest_poll_duration_seconds",
		Help:    "Wall time of one source poll including network.",
		Buckets: []float64{.05, .1, .25, .5, 1, 2.5, 5, 10},
	}, []string{"source"})

	EventsSeen = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_events_seen_total",
		Help: "Events returned by a source, before dedupe.",
	}, []string{"source"})

	EventsNew = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_events_new_total",
		Help: "Events that were genuinely new, after dedupe.",
	}, []string{"source"})

	// The gap between seen and new is the overlap cost of the safety window.
	// If new/seen approaches 1 the window is too narrow and we may be missing
	// events between polls.

	PublishDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "ingest_publish_duration_seconds",
		Help: "Time to append one message to the Redis Stream.",
		// Bucketed tightly around the 50 ms ingest budget.
		Buckets: []float64{.001, .0025, .005, .01, .025, .05, .1, .25},
	}, []string{"source"})

	PublishTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_publish_total",
		Help: "Messages appended to the Redis Stream, by outcome.",
	}, []string{"source", "change", "status"})

	WatermarkAge = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ingest_watermark_age_seconds",
		Help: "Age of the stored ingest cursor. Growing without bound means the poller is stalled.",
	}, []string{"source"})

	// Queue depth is the whole reason the availability tier uses a bounded pool
	// rather than a goroutine per event: an unbounded design has no number to
	// put here, so saturation is invisible until memory runs out.
	PoolQueueDepth = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ingest_pool_queue_depth",
		Help: "Jobs waiting in the availability worker pool.",
	}, []string{"source"})

	PollDropped = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_poll_dropped_total",
		Help: "Poll jobs shed because the worker queue was full. Non-zero means undersized workers or a slow upstream.",
	}, []string{"source"})

	WatchlistDue = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ingest_watchlist_due",
		Help: "Events returned by the most recent watchlist scheduling query.",
	}, []string{"source"})

	// A high unclassified rate means the fingerprint includes a field that
	// changes for reasons Classify does not model, and is burning polls.
	UnclassifiedChange = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_unclassified_change_total",
		Help: "Fingerprint changed but no rule explained it, so no drop was emitted.",
	}, []string{"source"})

	LinksCreated = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_cross_source_links_total",
		Help: "Events successfully linked across sources.",
	}, []string{"from", "to"})

	// Rejections are expected and healthy. A ratio approaching 100% over time
	// means the matcher's thresholds are too strict, or enumerate is not
	// covering the areas discovery is finding shows in.
	LinksRejected = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "ingest_cross_source_link_rejected_total",
		Help: "Candidate sets that produced no confident match.",
	}, []string{"from", "to"})
)
