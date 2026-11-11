package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestLoadEventsCanBeCreatedMutatedAndReset(t *testing.T) {
	s := &server{events: map[string]*event{}}
	ts := httptest.NewServer(s.routes())
	t.Cleanup(ts.Close)

	post(t, ts.URL+"/admin/load/events?prefix=phase1&count=2", http.StatusOK)
	if got := len(s.order); got != 2 {
		t.Fatalf("created %d events, want 2", got)
	}

	resp := post(t, ts.URL+"/admin/ticketed?id=phase1-00000", http.StatusOK)
	defer resp.Body.Close()
	var body struct {
		ChangedAt time.Time `json:"changed_at"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.ChangedAt.IsZero() {
		t.Fatal("mutation response omitted changed_at")
	}
	if !s.events["phase1-00000"].IsTicketed {
		t.Fatal("ticketed mutation did not update event")
	}

	post(t, ts.URL+"/admin/load/reset?prefix=phase1", http.StatusOK).Body.Close()
	if got := len(s.order); got != 0 {
		t.Fatalf("reset left %d events, want 0", got)
	}
}

func TestLoadEventsRejectInvalidInput(t *testing.T) {
	s := &server{events: map[string]*event{}}
	ts := httptest.NewServer(s.routes())
	t.Cleanup(ts.Close)

	post(t, ts.URL+"/admin/load/events?prefix=bad_prefix&count=2", http.StatusBadRequest).Body.Close()
	post(t, ts.URL+"/admin/load/events?prefix=phase&count=10001", http.StatusBadRequest).Body.Close()
}

func post(t *testing.T, url string, want int) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != want {
		resp.Body.Close()
		t.Fatalf("POST %s returned %d, want %d", url, resp.StatusCode, want)
	}
	return resp
}
