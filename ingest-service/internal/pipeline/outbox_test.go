package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"ticketdrop/ingest/internal/source"
	"ticketdrop/ingest/internal/store"
	"ticketdrop/ingest/internal/stream"
)

type fakeOutboxStore struct {
	markCalls int
	retries   []string
}

func (f *fakeOutboxStore) ClaimOutbox(
	context.Context,
	int,
	time.Duration,
) ([]store.OutboxItem, error) {
	return nil, nil
}

func (f *fakeOutboxStore) MarkOutboxPublished(context.Context, string) error {
	f.markCalls++
	if f.markCalls == 1 {
		return errors.New("database unavailable after publish")
	}
	return nil
}

func (f *fakeOutboxStore) RetryOutbox(
	_ context.Context,
	id, _ string,
	_ time.Duration,
) error {
	f.retries = append(f.retries, id)
	return nil
}

type fakeDropPublisher struct {
	ids []string
}

func (f *fakeDropPublisher) Publish(_ context.Context, drop stream.Drop) (string, error) {
	f.ids = append(f.ids, drop.ID)
	return drop.ID, nil
}

func TestOutboxRepublishesStableIDAfterMarkFailure(t *testing.T) {
	const dropID = "0192f0c0-0000-7000-8000-000000000001"
	payload, err := json.Marshal(stream.Drop{
		ID:          dropID,
		Change:      stream.ChangeNewTicketed,
		Event:       source.Event{Source: source.PlatformRA, SourceEventID: "90001"},
		Fingerprint: "abc",
		DetectedAt:  time.Now(),
	})
	if err != nil {
		t.Fatal(err)
	}
	item := store.OutboxItem{ID: dropID, Payload: payload, Attempts: 1}
	st := &fakeOutboxStore{}
	pub := &fakeDropPublisher{}
	dispatcher := NewOutboxDispatcher(
		st,
		pub,
		slog.New(slog.NewTextHandler(io.Discard, nil)),
	)

	if err := dispatcher.publish(context.Background(), item); err == nil {
		t.Fatal("first publish should report the failed completion write")
	}
	if err := dispatcher.publish(context.Background(), item); err != nil {
		t.Fatalf("republish: %v", err)
	}
	if len(pub.ids) != 2 || pub.ids[0] != dropID || pub.ids[1] != dropID {
		t.Fatalf("published IDs = %v, want stable ID twice", pub.ids)
	}
	if st.markCalls != 2 {
		t.Fatalf("mark calls = %d, want 2", st.markCalls)
	}
	if len(st.retries) != 0 {
		t.Fatalf("completion failure should wait for lease, retries = %v", st.retries)
	}
}

func TestOutboxBackoffCapsAtThirtyTwoSeconds(t *testing.T) {
	if got := outboxBackoff(1); got != time.Second {
		t.Fatalf("first backoff = %s", got)
	}
	if got := outboxBackoff(99); got != 32*time.Second {
		t.Fatalf("capped backoff = %s", got)
	}
}
