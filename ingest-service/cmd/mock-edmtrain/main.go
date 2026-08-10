// Command mock-edmtrain serves a stand-in for the EDMTrain Event Search API so
// the discovery tier can be exercised end to end before a real client key is
// approved.
//
// It reproduces the parts of the contract the adapter depends on: the
// {success, message, data} envelope, createdStartDate filtering, the
// livestreamInd filter, and 401 on a missing key.
//
//	go run ./cmd/mock-edmtrain -addr :8099
//	curl -X POST localhost:8099/admin/add   # inject a newly announced show
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"
)

type venue struct {
	ID        int64   `json:"id"`
	Name      string  `json:"name"`
	Location  string  `json:"location"`
	Address   string  `json:"address"`
	State     string  `json:"state"`
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
}

type artist struct {
	ID     int64  `json:"id"`
	Name   string `json:"name"`
	Link   string `json:"link"`
	B2BInd bool   `json:"b2bInd"`
}

type event struct {
	ID                 int64    `json:"id"`
	Link               string   `json:"link"`
	TicketLink         string   `json:"ticketLink"`
	Name               *string  `json:"name"`
	Ages               string   `json:"ages"`
	FestivalInd        bool     `json:"festivalInd"`
	LivestreamInd      bool     `json:"livestreamInd"`
	ElectronicGenreInd bool     `json:"electronicGenreInd"`
	OtherGenreInd      bool     `json:"otherGenreInd"`
	Date               string   `json:"date"`
	StartTime          *string  `json:"startTime"`
	EndTime            *string  `json:"endTime"`
	CreatedDate        string   `json:"createdDate"`
	Venue              venue    `json:"venue"`
	ArtistList         []artist `json:"artistList"`
}

type server struct {
	mu     sync.Mutex
	events []event
	nextID int64
}

func main() {
	addr := flag.String("addr", ":8099", "listen address")
	flag.Parse()

	s := &server{nextID: 9001}
	s.events = seed()

	mux := http.NewServeMux()
	mux.HandleFunc("/api/events", s.handleEvents)
	mux.HandleFunc("/admin/add", s.handleAdd)

	log.Printf("mock edmtrain listening on %s (%d seeded events)", *addr, len(s.events))
	log.Fatal(http.ListenAndServe(*addr, mux))
}

func (s *server) handleEvents(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("client") == "" {
		writeJSON(w, http.StatusUnauthorized, map[string]any{
			"success": false, "message": "Invalid client key", "data": nil,
		})
		return
	}

	var createdFrom time.Time
	if v := r.URL.Query().Get("createdStartDate"); v != "" {
		if t, err := time.Parse("2006-01-02", v); err == nil {
			createdFrom = t
		}
	}
	includeLivestreams := r.URL.Query().Get("livestreamInd") != "false"

	s.mu.Lock()
	defer s.mu.Unlock()

	out := make([]event, 0, len(s.events))
	for _, e := range s.events {
		if !includeLivestreams && e.LivestreamInd {
			continue
		}
		if !createdFrom.IsZero() {
			created, err := time.Parse("2006-01-02", e.CreatedDate)
			if err == nil && created.Before(createdFrom) {
				continue
			}
		}
		out = append(out, e)
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"success": true, "message": nil, "data": out,
	})
}

// handleAdd injects a freshly announced show, so a running poller can be
// observed picking up exactly one new event on its next tick.
func (s *server) handleAdd(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.nextID++
	e := event{
		ID:                 s.nextID,
		Link:               fmt.Sprintf("https://edmtrain.com/event/%d", s.nextID),
		TicketLink:         "https://link.dice.fm/mock-presale",
		Ages:               "21+",
		ElectronicGenreInd: true,
		Date:               time.Now().AddDate(0, 2, 0).Format("2006-01-02"),
		CreatedDate:        time.Now().UTC().Format("2006-01-02"),
		Venue: venue{
			ID: 5001, Name: "Spot Lite", Location: "Detroit", State: "Michigan",
			Address: "2905 Beaufait St", Latitude: 42.3548, Longitude: -83.0231,
		},
		ArtistList: []artist{{ID: 771, Name: "Prospa", Link: "https://edmtrain.com/prospa"}},
	}
	s.events = append(s.events, e)

	log.Printf("injected event %d", e.ID)
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "id": e.ID})
}

func seed() []event {
	today := time.Now().UTC().Format("2006-01-02")
	name := "Movement Detroit"
	return []event{
		{
			ID: 8001, Link: "https://edmtrain.com/event/8001",
			TicketLink: "https://link.dice.fm/seed-1", Ages: "18+",
			ElectronicGenreInd: true, Date: time.Now().AddDate(0, 1, 0).Format("2006-01-02"),
			CreatedDate: today,
			Venue: venue{
				ID: 5001, Name: "Spot Lite", Location: "Detroit", State: "Michigan",
				Latitude: 42.3548, Longitude: -83.0231,
			},
			ArtistList: []artist{
				{ID: 101, Name: "Omar S", Link: "https://edmtrain.com/omar-s", B2BInd: true},
				{ID: 102, Name: "Theo Parrish", Link: "https://edmtrain.com/theo-parrish"},
			},
		},
		{
			ID: 8002, Link: "https://edmtrain.com/event/8002",
			TicketLink: "https://link.dice.fm/seed-2", Name: &name, Ages: "All Ages",
			FestivalInd: true, ElectronicGenreInd: true,
			Date: time.Now().AddDate(0, 3, 0).Format("2006-01-02"), CreatedDate: today,
			Venue: venue{
				ID: 5002, Name: "Hart Plaza", Location: "Detroit", State: "Michigan",
				Latitude: 42.3277, Longitude: -83.0459,
			},
		},
		{
			// Excluded by the adapter: livestreams are filtered out.
			ID: 8003, Link: "https://edmtrain.com/event/8003",
			LivestreamInd: true, ElectronicGenreInd: true,
			Date: time.Now().AddDate(0, 1, 0).Format("2006-01-02"), CreatedDate: today,
			Venue:      venue{ID: 5003, Name: "Twitch"},
			ArtistList: []artist{{ID: 103, Name: "Livestream Act"}},
		},
	}
}

func writeJSON(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}
