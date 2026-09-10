package resolve

import (
	"testing"
	"time"
)

var showDate = time.Date(2026, 9, 12, 0, 0, 0, 0, time.UTC)

func TestNormalize(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{name: "lowercases", input: "Spot Lite", want: "spot lite"},
		{name: "strips diacritics", input: "Haus der Visionäre", want: "haus der visionare"},
		{name: "strips punctuation", input: "Omar-S / Theo Parrish!", want: "omar s theo parrish"},
		{name: "collapses whitespace", input: "  fabric   London  ", want: "fabric london"},
		{name: "keeps digits", input: "Club 90", want: "club 90"},
		{name: "empty", input: "", want: ""},
		{name: "punctuation only", input: "!!! ---", want: ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := normalize(tt.input); got != tt.want {
				t.Fatalf("normalize(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestVenueSimilarity(t *testing.T) {
	tests := []struct {
		name    string
		a, b    string
		wantMin float64
		wantMax float64
	}{
		{name: "exact", a: "Spot Lite", b: "Spot Lite", wantMin: 1, wantMax: 1},
		{name: "case and accent differences", a: "Haus der Visionäre", b: "haus der visionare", wantMin: 1, wantMax: 1},
		{
			// Sources disagree on whether the city belongs in the venue name.
			// That is not evidence of a different room.
			name: "containment counts as a match", a: "Spot Lite", b: "Spot Lite Detroit",
			wantMin: 1, wantMax: 1,
		},
		{name: "unrelated venues score low", a: "Spot Lite", b: "Hart Plaza", wantMin: 0, wantMax: 0.2},
		{name: "empty scores zero", a: "", b: "Spot Lite", wantMin: 0, wantMax: 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := venueSimilarity(tt.a, tt.b)
			if got < tt.wantMin || got > tt.wantMax {
				t.Fatalf("venueSimilarity(%q,%q) = %.2f, want in [%.2f,%.2f]",
					tt.a, tt.b, got, tt.wantMin, tt.wantMax)
			}
		})
	}
}

// Overlap coefficient rather than Jaccard: a thirty-act festival lineup that
// contains both of EDMTrain's two listed names is very likely the same event,
// but Jaccard would score that near zero purely on set-size difference.
func TestArtistOverlapHandlesLineupSizeMismatch(t *testing.T) {
	small := []string{"Omar S", "Theo Parrish"}
	festival := []string{
		"Omar S", "Theo Parrish", "Moodymann", "Carl Craig", "Jeff Mills",
		"Robert Hood", "DJ Holographic", "Waajeed", "Kyle Hall", "Rick Wilhite",
	}

	if got := artistOverlap(small, festival); got != 1 {
		t.Fatalf("subset lineup should score 1.0, got %.2f", got)
	}
	if got := artistOverlap([]string{"Prospa"}, festival); got != 0 {
		t.Fatalf("disjoint lineup should score 0, got %.2f", got)
	}
	if got := artistOverlap(nil, festival); got != 0 {
		t.Fatalf("empty lineup should score 0, got %.2f", got)
	}
}

func TestBest(t *testing.T) {
	tests := []struct {
		name       string
		query      Query
		candidates []Candidate
		wantID     string
		wantOK     bool
	}{
		{
			name: "clean match on venue, lineup, and date",
			query: Query{
				Title: "Prospa", VenueName: "Spot Lite",
				Date: showDate, Artists: []string{"Prospa"},
			},
			candidates: []Candidate{
				{SourceEventID: "90001", Title: "Prospa", VenueName: "Spot Lite",
					Date: showDate, Artists: []string{"Prospa"}},
				{SourceEventID: "90002", Title: "Foggy Sunday", VenueName: "TV Lounge",
					Date: showDate, Artists: []string{"Omar S"}},
			},
			wantID: "90001", wantOK: true,
		},
		{
			// Late-night sets get listed on either side of midnight.
			name: "date slack absorbs a one-day disagreement",
			query: Query{
				VenueName: "Spot Lite", Date: showDate,
				Artists: []string{"Prospa"},
			},
			candidates: []Candidate{
				{SourceEventID: "90001", VenueName: "Spot Lite",
					Date: showDate.Add(24 * time.Hour), Artists: []string{"Prospa"}},
			},
			wantID: "90001", wantOK: true,
		},
		{
			// The real-world worst case: a date-only source stores midnight UTC
			// while a timed source stores 22:00 local on the following listing
			// day. Same night, 46 h apart in storage.
			name: "date slack absorbs a date-only versus local-time offset",
			query: Query{
				VenueName: "Spot Lite", Date: showDate,
				Artists: []string{"Omar S", "Theo Parrish"},
			},
			candidates: []Candidate{
				{SourceEventID: "90002", Title: "Omar S / Theo Parrish",
					VenueName: "Spot Lite", Date: showDate.Add(46 * time.Hour),
					Artists: []string{"Omar S", "Theo Parrish"}},
			},
			wantID: "90002", wantOK: true,
		},
		{
			// The same show on a different night is not the same show, no matter
			// how well the lineup agrees.
			name: "a different week is rejected outright",
			query: Query{
				VenueName: "Spot Lite", Date: showDate,
				Artists: []string{"Prospa"},
			},
			candidates: []Candidate{
				{SourceEventID: "90001", VenueName: "Spot Lite",
					Date: showDate.AddDate(0, 0, 7), Artists: []string{"Prospa"}},
			},
			wantOK: false,
		},
		{
			// A residency produces near-identical candidates. Guessing between
			// them is exactly the false link this package refuses to make.
			name: "ambiguous candidates are rejected",
			query: Query{
				Title: "Interdimensional Transmissions", VenueName: "Spot Lite",
				Date: showDate, Artists: []string{"BMG"},
			},
			candidates: []Candidate{
				{SourceEventID: "90001", Title: "Interdimensional Transmissions",
					VenueName: "Spot Lite", Date: showDate, Artists: []string{"BMG"}},
				{SourceEventID: "90002", Title: "Interdimensional Transmissions",
					VenueName: "Spot Lite", Date: showDate, Artists: []string{"BMG"}},
			},
			wantOK: false,
		},
		{
			name: "weak agreement is rejected",
			query: Query{
				Title: "Prospa", VenueName: "Spot Lite",
				Date: showDate, Artists: []string{"Prospa"},
			},
			candidates: []Candidate{
				{SourceEventID: "90009", Title: "Latin Night", VenueName: "Hart Plaza",
					Date: showDate, Artists: []string{"DJ Iza"}},
			},
			wantOK: false,
		},
		{
			name:       "no candidates",
			query:      Query{VenueName: "Spot Lite", Date: showDate},
			candidates: nil,
			wantOK:     false,
		},
		{
			// EDMTrain frequently has no event title beyond the lineup. A
			// missing signal must degrade the score, not disqualify the match.
			name: "missing title still matches on venue and lineup",
			query: Query{
				VenueName: "Spot Lite", Date: showDate,
				Artists: []string{"Omar S", "Theo Parrish"},
			},
			candidates: []Candidate{
				{SourceEventID: "90001", Title: "Deep Detroit w/ Omar S",
					VenueName: "Spot Lite", Date: showDate,
					Artists: []string{"Omar S", "Theo Parrish"}},
			},
			wantID: "90001", wantOK: true,
		},
		{
			// Promoter branding on one side and none on the other must not sink
			// an otherwise unambiguous match.
			name: "promoter branding in the title is tolerated",
			query: Query{
				Title: "Prospa", VenueName: "Spot Lite",
				Date: showDate, Artists: []string{"Prospa"},
			},
			candidates: []Candidate{
				{SourceEventID: "90001",
					Title:     "Paxahau presents: Prospa (Live) at Spot Lite Detroit",
					VenueName: "Spot Lite Detroit", Date: showDate,
					Artists: []string{"Prospa"}},
			},
			wantID: "90001", wantOK: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := Best(tt.query, tt.candidates)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v (score %.2f, runner-up %.2f, id %q)",
					ok, tt.wantOK, got.Score, got.RunnerUp, got.Candidate.SourceEventID)
			}
			if ok && got.Candidate.SourceEventID != tt.wantID {
				t.Fatalf("matched %q, want %q", got.Candidate.SourceEventID, tt.wantID)
			}
		})
	}
}

func TestScoreIsBounded(t *testing.T) {
	cases := []struct {
		name  string
		query Query
		cand  Candidate
	}{
		{
			name:  "perfect agreement",
			query: Query{Title: "Prospa", VenueName: "Spot Lite", Artists: []string{"Prospa"}},
			cand:  Candidate{Title: "Prospa", VenueName: "Spot Lite", Artists: []string{"Prospa"}},
		},
		{
			name:  "total disagreement",
			query: Query{Title: "Prospa", VenueName: "Spot Lite", Artists: []string{"Prospa"}},
			cand:  Candidate{Title: "Latin Night", VenueName: "Hart Plaza", Artists: []string{"DJ Iza"}},
		},
		{name: "all fields empty", query: Query{}, cand: Candidate{}},
	}

	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			got := score(tt.query, tt.cand)
			if got < 0 || got > 1 {
				t.Fatalf("score = %.4f, must be within [0,1]", got)
			}
		})
	}
}
