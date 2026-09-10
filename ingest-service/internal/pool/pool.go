// Package pool provides a bounded worker pool for the availability tier.
//
// Why bounded rather than a goroutine per event: the availability loop is
// driven by a ticker that fires every ~2 s regardless of whether the previous
// round finished. If the upstream slows from 200 ms to 5 s — which is exactly
// what happens during a busy drop — unbounded spawning means several rounds are
// in flight at once and goroutines accumulate faster than they drain. That
// fails precisely when the system matters most, and it turns us into the
// thundering herd that gets an IP blocked.
//
// A fixed pool makes the backlog a single observable number, and makes the
// full-queue case an explicit decision instead of silent accumulation.
package pool

import (
	"context"
	"sync"
)

type Job func(context.Context)

type Pool struct {
	jobs    chan Job
	wg      sync.WaitGroup
	workers int

	closeOnce sync.Once
}

// New creates a pool with `workers` goroutines and a queue of `queue` jobs.
//
// Sizing is Little's Law, not guesswork: workers ≈ throughput × latency. At 200
// watchlisted events on a 2 s cadence (100 polls/sec) with a 200 ms upstream
// round trip, that is ~20 workers. Oversizing wastes memory and increases
// upstream concurrency; undersizing silently grows the queue.
func New(workers, queue int) *Pool {
	if workers < 1 {
		workers = 1
	}
	if queue < 1 {
		queue = 1
	}
	return &Pool{jobs: make(chan Job, queue), workers: workers}
}

func (p *Pool) Start(ctx context.Context) {
	for i := 0; i < p.workers; i++ {
		p.wg.Add(1)
		go func() {
			defer p.wg.Done()
			for job := range p.jobs {
				// Check cancellation between jobs rather than abandoning one
				// mid-flight: a half-finished poll that has already written to
				// the stream must still finish its bookkeeping.
				if ctx.Err() != nil {
					return
				}
				job(ctx)
			}
		}()
	}
}

// TrySubmit enqueues a job, returning false if the queue is full.
//
// Dropping is the correct behavior for time-sensitive polling, not a
// compromise. A poll job that has been queued for seconds is checking stale
// data, and the next tick will produce a fresher one. Blocking here would
// instead push backpressure onto the ticker and delay every remaining event in
// the round.
func (p *Pool) TrySubmit(job Job) bool {
	select {
	case p.jobs <- job:
		return true
	default:
		return false
	}
}

// Queued reports current backlog depth, for a Prometheus gauge. Depth that
// trends upward means workers or upstream latency need attention.
func (p *Pool) Queued() int { return len(p.jobs) }

// Close stops accepting work and waits for in-flight jobs to finish.
func (p *Pool) Close() {
	p.closeOnce.Do(func() { close(p.jobs) })
	p.wg.Wait()
}
