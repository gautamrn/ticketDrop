// Command mock-ra serves a stand-in for RA's GraphQL API so the availability
// tier can be exercised without polling the live site every two seconds during
// development.
//
// It implements only the two operations the adapter sends — eventListings and
// event(id) — dispatching on a marker in the query text rather than parsing
// GraphQL, which is enough to be a faithful test double and nothing more.
//
//	go run ./cmd/mock-ra -addr :8098
//	curl -X POST "localhost:8098/admin/presale?id=90001"   # open a presale
//	curl -X POST "localhost:8098/admin/ticketed?id=90002"  # flip isTicketed
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

type event struct {
	ID              string  `json:"id"`
	Title           string  `json:"title"`
	Date            string  `json:"date"`
	StartTime       string  `json:"startTime"`
	EndTime         *string `json:"endTime"`
	ContentURL      string  `json:"contentUrl"`
	IsTicketed      bool    `json:"isTicketed"`
	QueueItEnabled  bool    `json:"queueItEnabled"`
	Cost            string  `json:"cost"`
	Attending       int     `json:"attending"`
	InterestedCount int     `json:"interestedCount"`
	MinimumAge      *int    `json:"minimumAge"`
	DateUpdated     string  `json:"dateUpdated"`
	PresaleStatus   *string `json:"presaleStatus"`
	ResaleActive    bool    `json:"resaleActive"`
	Status          string  `json:"status"`
	Venue           *venue  `json:"venue"`
	Artists         []actor `json:"artists"`
	Genres          []genre `json:"genres"`
}

type venue struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	ContentURL string `json:"contentUrl"`
	Area       *area  `json:"area"`
}

type area struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type actor struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	ContentURL string `json:"contentUrl"`
}

type genre struct {
	Name string `json:"name"`
}

type server struct {
	mu     sync.Mutex
	events map[string]*event
	order  []string
}

func main() {
	addr := flag.String("addr", ":8098", "listen address")
	flag.Parse()

	s := &server{events: map[string]*event{}}
	s.seed()

	mux := http.NewServeMux()
	mux.HandleFunc("/graphql", s.handleGraphQL)
	mux.HandleFunc("/admin/presale", s.mutate(func(e *event) {
		active := "ACTIVE"
		e.PresaleStatus = &active
		e.Cost = "$25 - $40"
	}))
	mux.HandleFunc("/admin/ticketed", s.mutate(func(e *event) { e.IsTicketed = true }))
	mux.HandleFunc("/admin/resale", s.mutate(func(e *event) { e.ResaleActive = true }))
	mux.HandleFunc("/admin/flyer", s.mutate(func(e *event) {
		// Touches only dateUpdated: the fingerprint moves but nothing
		// classifies, which is the case that must NOT produce a drop.
	}))

	log.Printf("mock ra listening on %s (%d events)", *addr, len(s.order))
	log.Fatal(http.ListenAndServe(*addr, mux))
}

func (s *server) handleGraphQL(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	var req struct {
		Query     string         `json:"query"`
		Variables map[string]any `json:"variables"`
	}
	_ = json.Unmarshal(body, &req)

	if r.Header.Get("User-Agent") == "" {
		writeJSON(w, http.StatusForbidden, map[string]any{
			"errors": []map[string]string{{"message": "forbidden"}},
		})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	switch {
	case strings.Contains(req.Query, "eventListings"):
		rows := make([]map[string]any, 0, len(s.order))
		for _, id := range s.order {
			rows = append(rows, map[string]any{"event": s.events[id]})
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{
				"eventListings": map[string]any{
					"totalResults": len(rows),
					"data":         rows,
				},
			},
		})

	case strings.Contains(req.Query, "event(id:"), strings.Contains(req.Query, "event(id: $id)"):
		id, _ := req.Variables["id"].(string)
		e, ok := s.events[id]
		if !ok {
			writeJSON(w, http.StatusOK, map[string]any{
				"data": map[string]any{"event": nil},
			})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{"event": e},
		})

	default:
		writeJSON(w, http.StatusOK, map[string]any{
			"errors": []map[string]string{{"message": "unsupported operation"}},
		})
	}
}

func (s *server) mutate(apply func(*event)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := r.URL.Query().Get("id")
		s.mu.Lock()
		defer s.mu.Unlock()

		e, ok := s.events[id]
		if !ok {
			http.Error(w, "unknown event id", http.StatusNotFound)
			return
		}
		apply(e)
		// Any real edit on RA bumps dateUpdated, so the double must too.
		e.DateUpdated = time.Now().UTC().Format("2006-01-02T15:04:05.000")
		log.Printf("mutated %s: ticketed=%v presale=%v resale=%v",
			id, e.IsTicketed, deref(e.PresaleStatus), e.ResaleActive)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "id": id})
	}
}

func (s *server) seed() {
	now := time.Now().UTC()

	// Dates mirror cmd/mock-edmtrain's fixtures so the cross-source resolver has
	// something real to link. Doors at 22:00, built as a real time and formatted
	// with a real layout: embedding "22" in the layout string would be parsed as
	// a day-of-month component and emit garbage.
	mk := func(id, title, venueID, venueName string, monthsOut, attending int, ticketed bool) *event {
		showDate := now.AddDate(0, monthsOut, 0).Truncate(24 * time.Hour)
		doors := showDate.Add(22 * time.Hour)

		return &event{
			ID: id, Title: title,
			Date:        showDate.Format("2006-01-02"),
			StartTime:   doors.Format("2006-01-02T15:04:05.000"),
			ContentURL:  "/events/" + id,
			IsTicketed:  ticketed,
			Cost:        "",
			Attending:   attending,
			Status:      "LIVE",
			DateUpdated: now.Add(-48 * time.Hour).Format("2006-01-02T15:04:05.000"),
			Venue: &venue{
				// Distinct ids per venue. Reusing one id collapses every venue
				// into a single row via the (source, source_venue_id) upsert,
				// which is correct behavior but makes the fixture wrong.
				ID: venueID, Name: venueName,
				ContentURL: "/clubs/" + venueID,
				Area:       &area{ID: "19", Name: "Detroit"},
			},
			Genres: []genre{{Name: "house"}, {Name: "techno"}},
		}
	}

	// Matches mock-edmtrain's /admin/add event: Prospa at Spot Lite, two months
	// out. Promoter branding differs on purpose, to prove the matcher tolerates it.
	prospa := mk("90001", "Paxahau presents: Prospa (Live)", "5001", "Spot Lite", 2, 214, false)
	prospa.Artists = []actor{{ID: "771", Name: "Prospa", ContentURL: "/dj/prospa"}}

	// Matches mock-edmtrain's first seeded event, one month out.
	omar := mk("90002", "Omar S / Theo Parrish", "5001", "Spot Lite", 1, 96, false)
	omar.Artists = []actor{
		{ID: "101", Name: "Omar S", ContentURL: "/dj/omars"},
		{ID: "102", Name: "Theo Parrish", ContentURL: "/dj/theoparrish"},
	}

	// Deliberately three months off from EDMTrain's Movement fixture, so it must
	// NOT link: the same festival on a different date is a different night.
	queued := mk("90003", "Movement Detroit", "5003", "Hart Plaza", 1, 4820, true)
	// Carries a real lineup so this event can score high enough to reach the
	// reservation stage, which is the only way to prove that queueItEnabled
	// forces alert-only rather than merely being carried in the message.
	queued.Artists = []actor{{ID: "201", Name: "Carl Craig", ContentURL: "/dj/carlcraig"}}
	queued.QueueItEnabled = true // must be forced to alert-only downstream
	queued.Cost = "$180 - $320"

	for _, e := range []*event{prospa, omar, queued} {
		s.events[e.ID] = e
		s.order = append(s.order, e.ID)
	}
}

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}

func writeJSON(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		log.Printf("write: %v", fmt.Errorf("%w", err))
	}
}
