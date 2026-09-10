package pool

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestPoolRunsAllSubmittedJobs(t *testing.T) {
	tests := []struct {
		name    string
		workers int
		queue   int
		jobs    int
	}{
		{name: "single worker", workers: 1, queue: 16, jobs: 10},
		{name: "many workers", workers: 8, queue: 64, jobs: 50},
		{name: "workers exceed jobs", workers: 20, queue: 64, jobs: 3},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := New(tt.workers, tt.queue)
			p.Start(context.Background())

			var ran atomic.Int64
			var wg sync.WaitGroup
			for i := 0; i < tt.jobs; i++ {
				wg.Add(1)
				if !p.TrySubmit(func(context.Context) {
					defer wg.Done()
					ran.Add(1)
				}) {
					wg.Done()
					t.Fatalf("submit rejected with queue size %d", tt.queue)
				}
			}
			wg.Wait()
			p.Close()

			if got := ran.Load(); got != int64(tt.jobs) {
				t.Fatalf("ran %d jobs, want %d", got, tt.jobs)
			}
		})
	}
}

// The point of the pool is that a full queue is an explicit decision rather
// than silent accumulation. Shedding stale poll work is correct: the next tick
// produces a fresher job for the same event.
func TestTrySubmitShedsWhenFull(t *testing.T) {
	p := New(1, 2)

	release := make(chan struct{})
	blocker := func(context.Context) { <-release }

	// Occupy the single worker, then fill the queue exactly.
	if !p.TrySubmit(blocker) {
		t.Fatal("first submit should be accepted")
	}
	p.Start(context.Background())
	// Give the worker a moment to pick up the blocking job so the queue is empty
	// again before we fill it deliberately.
	time.Sleep(50 * time.Millisecond)

	accepted := 0
	for i := 0; i < 10; i++ {
		if p.TrySubmit(func(context.Context) {}) {
			accepted++
		}
	}
	if accepted > 2 {
		t.Fatalf("accepted %d jobs into a queue of 2; the bound is not enforced", accepted)
	}
	if accepted == 0 {
		t.Fatal("accepted nothing; the queue should hold up to its capacity")
	}

	close(release)
	p.Close()
}

func TestQueuedReportsDepth(t *testing.T) {
	p := New(1, 8)

	release := make(chan struct{})
	p.TrySubmit(func(context.Context) { <-release })
	p.Start(context.Background())
	time.Sleep(50 * time.Millisecond)

	for i := 0; i < 3; i++ {
		p.TrySubmit(func(context.Context) {})
	}
	if got := p.Queued(); got != 3 {
		t.Fatalf("Queued() = %d, want 3", got)
	}

	close(release)
	p.Close()
}

// Cancellation must not abandon a job midway: a poll that has already written to
// the stream still needs to finish its bookkeeping. The pool checks between
// jobs, so an in-flight job always completes.
func TestInFlightJobCompletesAfterCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	p := New(1, 4)

	started := make(chan struct{})
	var finished atomic.Bool

	p.TrySubmit(func(context.Context) {
		close(started)
		time.Sleep(100 * time.Millisecond)
		finished.Store(true)
	})
	p.Start(ctx)

	<-started
	cancel()
	p.Close()

	if !finished.Load() {
		t.Fatal("in-flight job was abandoned on cancellation")
	}
}

func TestNewClampsInvalidSizes(t *testing.T) {
	tests := []struct {
		name           string
		workers, queue int
	}{
		{name: "zero workers", workers: 0, queue: 4},
		{name: "negative workers", workers: -5, queue: 4},
		{name: "zero queue", workers: 2, queue: 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := New(tt.workers, tt.queue)
			p.Start(context.Background())

			done := make(chan struct{})
			if !p.TrySubmit(func(context.Context) { close(done) }) {
				t.Fatal("clamped pool rejected the first job")
			}
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("clamped pool never ran the job")
			}
			p.Close()
		})
	}
}

func TestCloseIsIdempotent(t *testing.T) {
	p := New(2, 4)
	p.Start(context.Background())
	p.Close()
	p.Close() // must not panic on a double close of the jobs channel
}
