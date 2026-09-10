package ra

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"ticketdrop/ingest/internal/source"
)

func TestAbsoluteURL(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{name: "site relative path is expanded", input: "/events/90001", want: "https://ra.co/events/90001"},
		{name: "bare path is expanded", input: "dj/prospa", want: "https://ra.co/dj/prospa"},
		{name: "absolute https is untouched", input: "https://ra.co/events/1", want: "https://ra.co/events/1"},
		{name: "absolute http is untouched", input: "http://example.com/x", want: "http://example.com/x"},
		{name: "empty stays empty", input: "", want: ""},
		{name: "whitespace only stays empty", input: "   ", want: ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := absoluteURL(tt.input); got != tt.want {
				t.Fatalf("absoluteURL(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestParseTime(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
		isNil bool
	}{
		{
			// RA returns LocalDateTime with milliseconds and no zone.
			name:  "local date time with millis",
			input: "2026-09-12T22:00:00.000",
			want:  "2026-09-12T22:00:00Z",
		},
		{name: "rfc3339", input: "2026-09-12T22:00:00Z", want: "2026-09-12T22:00:00Z"},
		{name: "date only", input: "2026-09-12", want: "2026-09-12T00:00:00Z"},
		{name: "empty is nil", input: "", isNil: true},
		{name: "garbage is nil", input: "soon", isNil: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := parseTime(tt.input)
			if tt.isNil {
				if got != nil {
					t.Fatalf("want nil, got %v", got)
				}
				return
			}
			if got == nil {
				t.Fatal("want a time, got nil")
			}
			if s := got.Format(time.RFC3339); s != tt.want {
				t.Fatalf("got %s, want %s", s, tt.want)
			}
		})
	}
}

func TestParseAreaIDs(t *testing.T) {
	tests := []struct {
		name  string
		input []string
		want  int
	}{
		{name: "numeric ids pass", input: []string{"19", "13", "34"}, want: 3},
		{name: "whitespace is trimmed", input: []string{" 19 ", "13"}, want: 2},
		{name: "non numeric is dropped", input: []string{"19", "detroit", ""}, want: 1},
		{name: "empty input", input: nil, want: 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := len(ParseAreaIDs(tt.input)); got != tt.want {
				t.Fatalf("got %d ids, want %d", got, tt.want)
			}
		})
	}
}

const listingResponse = `{"data":{"eventListings":{"totalResults":1,"data":[{"event":{
  "id":"90001","title":" Prospa ","date":"2026-09-12",
  "startTime":"2026-09-12T22:00:00.000","contentUrl":"/events/90001",
  "isTicketed":false,"queueItEnabled":false,"cost":"","attending":214,
  "interestedCount":30,"minimumAge":21,"dateUpdated":"2026-08-01T10:00:00.000",
  "venue":{"id":"5001","name":"Spot Lite","contentUrl":"/clubs/spotlite","area":{"id":"19","name":"Detroit"}},
  "artists":[{"id":"771","name":"Prospa","contentUrl":"/dj/prospa"},{"id":"772","name":"  "}],
  "genres":[{"name":"house"},{"name":"techno"}]}}]}}}`

func TestEnumerateMapsEvents(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") == "" {
			t.Error("RA requires a browser User-Agent; the client did not send one")
		}
		if r.Header.Get("Referer") == "" {
			t.Error("client did not send a Referer")
		}
		_, _ = w.Write([]byte(listingResponse))
	}))
	defer srv.Close()

	a := NewAdapter(NewClient(srv.URL, "test-agent", 5*time.Second), []string{"19"}, 60*24*time.Hour)
	events, err := a.Enumerate(context.Background(), 50)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("want 1 event, got %d", len(events))
	}

	ev := events[0]
	if ev.Source != source.PlatformRA {
		t.Errorf("source = %q", ev.Source)
	}
	if ev.Title != "Prospa" {
		t.Errorf("title = %q, want trimmed %q", ev.Title, "Prospa")
	}
	if ev.ContentURL != "https://ra.co/events/90001" {
		t.Errorf("contentURL = %q, want absolute", ev.ContentURL)
	}
	if ev.Venue == nil || ev.Venue.AreaName != "Detroit" {
		t.Errorf("venue = %+v", ev.Venue)
	}
	if len(ev.Artists) != 1 {
		t.Errorf("want blank artist names dropped, got %d artists", len(ev.Artists))
	}
	if len(ev.Genres) != 2 {
		t.Errorf("genres = %v", ev.Genres)
	}
	if ev.StartsAt == nil {
		t.Error("startsAt should be parsed from startTime")
	}
}

func TestSnapshotReadsSaleState(t *testing.T) {
	const detail = `{"data":{"event":{
	  "id":"90001","title":"Prospa","isTicketed":true,"queueItEnabled":false,
	  "cost":"$25 - $40","presaleStatus":"ACTIVE","resaleActive":false,
	  "status":"LIVE","attending":250,"interestedCount":40,
	  "dateUpdated":"2026-08-09T18:00:00.000"}}}`

	var sentQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		sentQuery = string(body)
		_, _ = w.Write([]byte(detail))
	}))
	defer srv.Close()

	a := NewAdapter(NewClient(srv.URL, "test-agent", 5*time.Second), []string{"19"}, time.Hour)
	snap, err := a.Snapshot(context.Background(), "90001")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Event.tickets returns [] unauthenticated, so requesting it would add
	// payload while implying a capability we do not have.
	if strings.Contains(sentQuery, "tickets") {
		t.Error("detail query must not request Event.tickets; it is empty without auth")
	}

	if !snap.IsTicketed {
		t.Error("isTicketed not read")
	}
	if snap.PresaleStatus != "ACTIVE" {
		t.Errorf("presaleStatus = %q", snap.PresaleStatus)
	}
	if snap.Cost != "$25 - $40" {
		t.Errorf("cost = %q", snap.Cost)
	}
	if snap.EventStatus != "LIVE" {
		t.Errorf("status = %q", snap.EventStatus)
	}
	if snap.Attending == nil || *snap.Attending != 250 {
		t.Errorf("attending = %v", snap.Attending)
	}
	if snap.DateUpdated == nil {
		t.Error("dateUpdated not parsed")
	}
	if snap.ObservedAt.IsZero() {
		t.Error("observedAt must be set; latency accounting depends on it")
	}
	if len(snap.Raw) == 0 {
		t.Error("raw payload not preserved")
	}
}

func TestClientErrorMapping(t *testing.T) {
	tests := []struct {
		name       string
		status     int
		body       string
		wantErr    error
		wantErrSub string
	}{
		{
			name:    "429 maps to rate limited",
			status:  http.StatusTooManyRequests,
			body:    `{}`,
			wantErr: ErrRateLimited,
		},
		{
			name:    "403 maps to blocked",
			status:  http.StatusForbidden,
			body:    `{}`,
			wantErr: ErrBlocked,
		},
		{
			// GraphQL reports application errors inside a 200 body, so status
			// alone is not a success signal.
			name:       "graphql errors in a 200 body are surfaced",
			status:     http.StatusOK,
			body:       `{"errors":[{"message":"Cannot query field \"nope\""}]}`,
			wantErrSub: "Cannot query field",
		},
		{
			name:    "null event maps to not found",
			status:  http.StatusOK,
			body:    `{"data":{"event":null}}`,
			wantErr: ErrNotFound,
		},
		{
			name:       "malformed json",
			status:     http.StatusOK,
			body:       `{"data":{`,
			wantErrSub: "decode",
		},
		{
			name:       "unexpected 500",
			status:     http.StatusInternalServerError,
			body:       `boom`,
			wantErrSub: "unexpected status 500",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tt.status)
				_, _ = w.Write([]byte(tt.body))
			}))
			defer srv.Close()

			c := NewClient(srv.URL, "test-agent", 2*time.Second)
			_, _, err := c.Event(context.Background(), "90001")

			if tt.wantErr != nil {
				if !errors.Is(err, tt.wantErr) {
					t.Fatalf("want %v, got %v", tt.wantErr, err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tt.wantErrSub) {
				t.Fatalf("want error containing %q, got %v", tt.wantErrSub, err)
			}
		})
	}
}

func TestAdapterMetadata(t *testing.T) {
	a := NewAdapter(nil, nil, time.Hour)
	if a.Name() != source.PlatformRA {
		t.Errorf("Name() = %q", a.Name())
	}
	if a.Tier() != source.TierAvailability {
		t.Errorf("Tier() = %q, RA must be the availability tier", a.Tier())
	}
}
