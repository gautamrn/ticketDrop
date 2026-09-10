package fingerprint

import (
	"testing"
	"time"

	"ticketdrop/ingest/internal/source"
)

func base() source.Snapshot {
	return source.Snapshot{
		SourceEventID: "90001",
		IsTicketed:    false,
		Cost:          "",
		PresaleStatus: "",
		ResaleActive:  false,
		QueueIt:       false,
		EventStatus:   "LIVE",
	}
}

func TestComputeStability(t *testing.T) {
	updated := time.Date(2026, 8, 9, 12, 0, 0, 0, time.UTC)

	tests := []struct {
		name    string
		mutate  func(*source.Snapshot)
		differs bool
	}{
		{
			name:    "identical snapshot hashes the same",
			mutate:  func(*source.Snapshot) {},
			differs: false,
		},
		{
			// attending ticks constantly as people save an event. Including it
			// would make the fingerprint change on nearly every poll and
			// destroy its value as a cheap "nothing happened" filter.
			name:    "attending is excluded",
			mutate:  func(s *source.Snapshot) { n := 999; s.Attending = &n },
			differs: false,
		},
		{
			name:    "interestedCount is excluded",
			mutate:  func(s *source.Snapshot) { n := 42; s.InterestedCount = &n },
			differs: false,
		},
		{
			name:    "observedAt is excluded",
			mutate:  func(s *source.Snapshot) { s.ObservedAt = time.Now() },
			differs: false,
		},
		{
			// Cosmetic reformatting of the same price must not read as a change.
			name:    "cost whitespace and case are normalized",
			mutate:  func(s *source.Snapshot) { s.Cost = "  £15   -   £20  " },
			differs: false,
		},
		{name: "isTicketed changes the hash", mutate: func(s *source.Snapshot) { s.IsTicketed = true }, differs: true},
		{name: "presaleStatus changes the hash", mutate: func(s *source.Snapshot) { s.PresaleStatus = "ACTIVE" }, differs: true},
		{name: "resaleActive changes the hash", mutate: func(s *source.Snapshot) { s.ResaleActive = true }, differs: true},
		{name: "queueIt changes the hash", mutate: func(s *source.Snapshot) { s.QueueIt = true }, differs: true},
		{name: "eventStatus changes the hash", mutate: func(s *source.Snapshot) { s.EventStatus = "PENDING" }, differs: true},
		{name: "dateUpdated changes the hash", mutate: func(s *source.Snapshot) { s.DateUpdated = &updated }, differs: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			orig := base()
			orig.Cost = "£15 - £20"
			ref := Compute(orig)

			mutated := orig
			tt.mutate(&mutated)
			got := Compute(mutated)

			if tt.differs && got == ref {
				t.Fatal("expected fingerprint to change, it did not")
			}
			if !tt.differs && got != ref {
				t.Fatal("expected fingerprint to be stable, it changed")
			}
		})
	}
}

// Field values must not be able to bleed across boundaries and produce the same
// hash for different states.
func TestComputeNoFieldBoundaryCollision(t *testing.T) {
	a := base()
	a.Cost = "AB"
	a.PresaleStatus = "C"

	b := base()
	b.Cost = "A"
	b.PresaleStatus = "BC"

	if Compute(a) == Compute(b) {
		t.Fatal("distinct field splits produced the same fingerprint")
	}
}

func TestClassify(t *testing.T) {
	tests := []struct {
		name       string
		prev, cur  func(*source.Snapshot)
		wantChange string
		wantOK     bool
	}{
		{
			name:       "presale opening is the highest value signal",
			prev:       func(s *source.Snapshot) { s.IsTicketed = true },
			cur:        func(s *source.Snapshot) { s.IsTicketed = true; s.PresaleStatus = "ACTIVE" },
			wantChange: ChangeTierAvailable,
			wantOK:     true,
		},
		{
			// A presale usually reveals price text on the same poll. Presale
			// must win, otherwise the cost heuristic mislabels the event.
			name: "presale wins over a simultaneous cost change",
			prev: func(s *source.Snapshot) { s.IsTicketed = true; s.Cost = "TBA" },
			cur: func(s *source.Snapshot) {
				s.IsTicketed = true
				s.Cost = "$25 - $40"
				s.PresaleStatus = "ACTIVE"
			},
			wantChange: ChangeTierAvailable,
			wantOK:     true,
		},
		{
			name:       "tickets appearing is new_ticketed",
			prev:       func(*source.Snapshot) {},
			cur:        func(s *source.Snapshot) { s.IsTicketed = true },
			wantChange: ChangeNewTicketed,
			wantOK:     true,
		},
		{
			name:       "resale opening is returned_to_sale",
			prev:       func(s *source.Snapshot) { s.IsTicketed = true },
			cur:        func(s *source.Snapshot) { s.IsTicketed = true; s.ResaleActive = true },
			wantChange: ChangeReturnedToSale,
			wantOK:     true,
		},
		{
			name:       "cost change while ticketed implies a tier change",
			prev:       func(s *source.Snapshot) { s.IsTicketed = true; s.Cost = "$20" },
			cur:        func(s *source.Snapshot) { s.IsTicketed = true; s.Cost = "$20 - $35" },
			wantChange: ChangeTierAvailable,
			wantOK:     true,
		},
		{
			// Cost text on an unticketed listing is an announcement detail, not
			// a sale event.
			name:   "cost change while unticketed is not a drop",
			prev:   func(s *source.Snapshot) { s.Cost = "TBA" },
			cur:    func(s *source.Snapshot) { s.Cost = "free before 11" },
			wantOK: false,
		},
		{
			name:       "listing going live while ticketed is new_ticketed",
			prev:       func(s *source.Snapshot) { s.EventStatus = "PENDING"; s.IsTicketed = true },
			cur:        func(s *source.Snapshot) { s.EventStatus = "LIVE"; s.IsTicketed = true },
			wantChange: ChangeNewTicketed,
			wantOK:     true,
		},
		{
			// The most important negative case: dateUpdated moves for typo
			// fixes and flyer swaps. Alerting on it would train the user to
			// ignore alerts.
			name: "dateUpdated alone is not a drop",
			prev: func(s *source.Snapshot) { s.IsTicketed = true },
			cur: func(s *source.Snapshot) {
				s.IsTicketed = true
				t := time.Now().UTC()
				s.DateUpdated = &t
			},
			wantOK: false,
		},
		{
			name:   "presale completing is not a drop",
			prev:   func(s *source.Snapshot) { s.IsTicketed = true; s.PresaleStatus = "ACTIVE" },
			cur:    func(s *source.Snapshot) { s.IsTicketed = true; s.PresaleStatus = "COMPLETED" },
			wantOK: false,
		},
		{
			name:   "tickets disappearing is not a drop",
			prev:   func(s *source.Snapshot) { s.IsTicketed = true },
			cur:    func(*source.Snapshot) {},
			wantOK: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			prev, cur := base(), base()
			tt.prev(&prev)
			tt.cur(&cur)

			change, ok := Classify(prev, cur)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v (change=%q)", ok, tt.wantOK, change)
			}
			if ok && change != tt.wantChange {
				t.Fatalf("change = %q, want %q", change, tt.wantChange)
			}
		})
	}
}

// Classification is only ever consulted after the hash moves, so a transition
// Classify recognizes must also change the fingerprint. Otherwise the rule is
// unreachable.
func TestClassifiableTransitionsAlsoChangeFingerprint(t *testing.T) {
	transitions := []struct {
		name string
		cur  func(*source.Snapshot)
	}{
		{"ticketed", func(s *source.Snapshot) { s.IsTicketed = true }},
		{"presale", func(s *source.Snapshot) { s.PresaleStatus = "ACTIVE" }},
		{"resale", func(s *source.Snapshot) { s.ResaleActive = true }},
	}

	for _, tt := range transitions {
		t.Run(tt.name, func(t *testing.T) {
			prev := base()
			prev.IsTicketed = true
			cur := prev
			tt.cur(&cur)

			if _, ok := Classify(prev, cur); !ok {
				t.Skip("not classifiable from this baseline")
			}
			if Compute(prev) == Compute(cur) {
				t.Fatal("classifiable transition did not change the fingerprint; rule is unreachable")
			}
		})
	}
}
