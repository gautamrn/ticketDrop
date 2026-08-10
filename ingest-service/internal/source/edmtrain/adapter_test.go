package edmtrain

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"ticketdrop/ingest/internal/source"
)

func TestAdapterFiltersEvents(t *testing.T) {
	tests := []struct {
		name               string
		event              APIEvent
		includeLivestreams bool
		want               bool
	}{
		{
			name:  "electronic show is included",
			event: APIEvent{ID: 1, ElectronicGenreInd: true},
			want:  true,
		},
		{
			name:  "non-electronic show is excluded",
			event: APIEvent{ID: 2, ElectronicGenreInd: false},
			want:  false,
		},
		{
			name:  "livestream excluded by default",
			event: APIEvent{ID: 3, ElectronicGenreInd: true, LivestreamInd: true},
			want:  false,
		},
		{
			name:               "livestream included when configured",
			event:              APIEvent{ID: 4, ElectronicGenreInd: true, LivestreamInd: true},
			includeLivestreams: true,
			want:               true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			a := NewAdapter(nil, nil, tt.includeLivestreams)
			if got := a.include(tt.event); got != tt.want {
				t.Fatalf("include = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestAdapterTitle(t *testing.T) {
	tests := []struct {
		name  string
		event APIEvent
		want  string
	}{
		{
			name:  "explicit name wins",
			event: APIEvent{ID: 1, Name: "Movement Detroit"},
			want:  "Movement Detroit",
		},
		{
			// Most EDMTrain events have no name, so the lineup is the title.
			name: "falls back to lineup",
			event: APIEvent{ID: 2, ArtistList: []APIArtist{
				{Name: "Prospa"}, {Name: "Omar S"},
			}},
			want: "Prospa, Omar S",
		},
		{
			name: "long lineup is truncated",
			event: APIEvent{ID: 3, ArtistList: []APIArtist{
				{Name: "A"}, {Name: "B"}, {Name: "C"}, {Name: "D"}, {Name: "E"},
			}},
			want: "A, B, C, D + more",
		},
		{
			name:  "no name and no lineup falls back to id",
			event: APIEvent{ID: 42},
			want:  "EDMTrain event 42",
		},
		{
			name:  "blank artist names are skipped",
			event: APIEvent{ID: 5, ArtistList: []APIArtist{{Name: "  "}, {Name: "Prospa"}}},
			want:  "Prospa",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := title(tt.event); got != tt.want {
				t.Fatalf("title = %q, want %q", got, tt.want)
			}
		})
	}
}

// createdDate granularity is not guaranteed by the docs, so every plausible
// form must parse rather than silently yielding a nil announcement time.
func TestParseTime(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
		isNil bool
	}{
		{name: "date only", input: "2026-08-09", want: "2026-08-09T00:00:00Z"},
		{name: "space separated timestamp", input: "2026-08-09 17:45:00", want: "2026-08-09T17:45:00Z"},
		{name: "rfc3339", input: "2026-08-09T17:45:00Z", want: "2026-08-09T17:45:00Z"},
		{name: "empty is nil", input: "", isNil: true},
		{name: "garbage is nil", input: "not-a-date", isNil: true},
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
				t.Fatalf("want %s, got %s", tt.want, s)
			}
		})
	}
}

func TestParseAges(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  int
		isNil bool
	}{
		{name: "21+", input: "21+", want: 21},
		{name: "18 and over", input: "18 and over", want: 18},
		{name: "all ages is nil", input: "All Ages", isNil: true},
		{name: "empty is nil", input: "", isNil: true},
		{name: "implausible age is nil", input: "2026", isNil: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := parseAges(tt.input)
			if tt.isNil {
				if got != nil {
					t.Fatalf("want nil, got %d", *got)
				}
				return
			}
			if got == nil || *got != tt.want {
				t.Fatalf("want %d, got %v", tt.want, got)
			}
		})
	}
}

func TestAdapterMapsFullEvent(t *testing.T) {
	body := `{"success":true,"data":[{
		"id":8001,
		"link":"https://edmtrain.com/event/8001",
		"ticketLink":"https://link.dice.fm/x",
		"name":null,
		"ages":"21+",
		"electronicGenreInd":true,
		"date":"2026-09-12",
		"createdDate":"2026-08-09",
		"venue":{"id":5001,"name":"Spot Lite","location":"Detroit","state":"Michigan","latitude":42.35,"longitude":-83.02},
		"artistList":[
			{"id":101,"name":"Omar S","link":"https://edmtrain.com/omar-s","b2bInd":true},
			{"id":102,"name":"Theo Parrish"}
		]
	}]}`

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()

	a := NewAdapter(NewClient(srv.URL, "k", 5*time.Second), []string{"36"}, false)
	events, err := a.Discover(context.Background(), time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("want 1 event, got %d", len(events))
	}

	ev := events[0]
	if ev.Source != source.PlatformEDMTrain {
		t.Errorf("source = %q", ev.Source)
	}
	if ev.SourceEventID != "8001" {
		t.Errorf("source event id = %q", ev.SourceEventID)
	}
	if ev.Title != "Omar S, Theo Parrish" {
		t.Errorf("title = %q", ev.Title)
	}
	if !ev.IsTicketed {
		t.Error("a ticketLink should mark the event ticketed")
	}
	if ev.Venue == nil || ev.Venue.AreaName != "Detroit, Michigan" {
		t.Errorf("venue = %+v", ev.Venue)
	}
	if len(ev.Artists) != 2 {
		t.Fatalf("want 2 artists, got %d", len(ev.Artists))
	}
	if !ev.Artists[0].B2BWithNext {
		t.Error("b2bInd should carry through to the lineup")
	}
	if ev.AnnouncedAt == nil {
		t.Error("createdDate should populate AnnouncedAt")
	}
	if ev.MinimumAge == nil || *ev.MinimumAge != 21 {
		t.Errorf("minimum age = %v", ev.MinimumAge)
	}
	// raw must survive so an upstream field change is a backfill, not a loss.
	if len(ev.Raw) == 0 || string(ev.Raw) == "null" {
		t.Error("raw payload was not preserved")
	}
}

func TestAdapterMetadata(t *testing.T) {
	a := NewAdapter(nil, nil, false)
	if a.Name() != source.PlatformEDMTrain {
		t.Errorf("Name() = %q", a.Name())
	}
	if a.Tier() != source.TierDiscovery {
		t.Errorf("Tier() = %q, EDMTrain must not join the availability tier", a.Tier())
	}
}
