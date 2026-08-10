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
)
