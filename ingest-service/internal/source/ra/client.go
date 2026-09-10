// Package ra wraps Resident Advisor's public GraphQL API.
//
// This is RA's own internal API. There is no key, no developer agreement, and
// no stability guarantee, so every field name here is modeled from live
// introspection rather than from a contract. It also means the only governing
// document is RA's site terms: we send a real User-Agent and Referer because
// the endpoint requires them, poll conservatively, and never attempt to read
// anything a signed-out browser could not.
package ra

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

var (
	// ErrRateLimited means back off. RA does not document limits, so any 429
	// or 403 is treated as a hard signal rather than something to retry into.
	ErrRateLimited = errors.New("ra: rate limited")
	ErrBlocked     = errors.New("ra: request blocked")
	ErrNotFound    = errors.New("ra: event not found")
)

const (
	defaultEndpoint  = "https://ra.co/graphql"
	maxResponseBytes = 16 << 20
)

type Client struct {
	endpoint  string
	userAgent string
	http      *http.Client
}

func NewClient(endpoint, userAgent string, timeout time.Duration) *Client {
	if endpoint == "" {
		endpoint = defaultEndpoint
	}
	return &Client{
		endpoint:  endpoint,
		userAgent: userAgent,
		http: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				// One shared client with keep-alives: reconnecting per poll
				// would add a TLS handshake to every request, which is a large
				// fraction of a 2 s budget.
				MaxIdleConns:        32,
				MaxIdleConnsPerHost: 32,
				IdleConnTimeout:     90 * time.Second,
			},
		},
	}
}

type gqlError struct {
	Message string `json:"message"`
}

type gqlResponse struct {
	Data   json.RawMessage `json:"data"`
	Errors []gqlError      `json:"errors"`
}

func (c *Client) do(ctx context.Context, query string, vars map[string]any, out any) (json.RawMessage, error) {
	payload, err := json.Marshal(map[string]any{"query": query, "variables": vars})
	if err != nil {
		return nil, fmt.Errorf("ra: marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("ra: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", c.userAgent)
	req.Header.Set("Referer", "https://ra.co/")
	req.Header.Set("Origin", "https://ra.co")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ra: request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, fmt.Errorf("ra: read body: %w", err)
	}

	switch {
	case resp.StatusCode == http.StatusTooManyRequests:
		return nil, ErrRateLimited
	case resp.StatusCode == http.StatusForbidden:
		return nil, ErrBlocked
	case resp.StatusCode >= 300:
		return nil, fmt.Errorf("ra: unexpected status %d: %s", resp.StatusCode, snippet(body))
	}

	var envelope gqlResponse
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("ra: decode: %w: %s", err, snippet(body))
	}
	// GraphQL reports application errors in a 200 body, so status alone is not
	// a success signal.
	if len(envelope.Errors) > 0 {
		return nil, fmt.Errorf("ra: graphql error: %s", envelope.Errors[0].Message)
	}
	if out != nil {
		if err := json.Unmarshal(envelope.Data, out); err != nil {
			return nil, fmt.Errorf("ra: decode data: %w", err)
		}
	}
	return envelope.Data, nil
}

const listingsQuery = `
query TDListings($filters: FilterInputDtoInput, $pageSize: Int, $page: Int) {
  eventListings(filters: $filters, pageSize: $pageSize, page: $page,
                sort: { listingDate: { order: ASCENDING } }) {
    totalResults
    data {
      event {
        id title date startTime endTime contentUrl isTicketed queueItEnabled
        cost attending interestedCount minimumAge dateUpdated
        venue { id name contentUrl area { id name } }
        artists { id name contentUrl }
        genres { name }
      }
    }
  }
}`

type listingsResult struct {
	EventListings struct {
		TotalResults int `json:"totalResults"`
		Data         []struct {
			Event APIEvent `json:"event"`
		} `json:"data"`
	} `json:"eventListings"`
}

// APIEvent is the subset of RA's Event type we read. Fields absent here are
// either promoter-gated or irrelevant; see ARCHITECTURE.md §1.2.
type APIEvent struct {
	ID              string      `json:"id"`
	Title           string      `json:"title"`
	Date            string      `json:"date"`
	StartTime       string      `json:"startTime"`
	EndTime         string      `json:"endTime"`
	ContentURL      string      `json:"contentUrl"`
	IsTicketed      bool        `json:"isTicketed"`
	QueueItEnabled  bool        `json:"queueItEnabled"`
	Cost            string      `json:"cost"`
	Attending       *int        `json:"attending"`
	InterestedCount *int        `json:"interestedCount"`
	MinimumAge      *int        `json:"minimumAge"`
	DateUpdated     string      `json:"dateUpdated"`
	PresaleStatus   string      `json:"presaleStatus"`
	ResaleActive    bool        `json:"resaleActive"`
	Status          string      `json:"status"`
	Venue           *APIVenue   `json:"venue"`
	Artists         []APIArtist `json:"artists"`
	Genres          []APIGenre  `json:"genres"`
}

type APIVenue struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	ContentURL string `json:"contentUrl"`
	Area       *struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	} `json:"area"`
}

type APIArtist struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	ContentURL string `json:"contentUrl"`
}

type APIGenre struct {
	Name string `json:"name"`
}

// Listings enumerates events in an area over a date window. This is how we
// learn RA event ids at all; it is not the discovery tier, which is EDMTrain's
// job. Without enumeration there is nothing for the availability loop to poll.
func (c *Client) Listings(ctx context.Context, areaID string, from, to time.Time, pageSize, page int) ([]APIEvent, int, error) {
	vars := map[string]any{
		"filters": map[string]any{
			"areas": map[string]any{"eq": areaID},
			"listingDate": map[string]any{
				"gte": from.UTC().Format("2006-01-02"),
				"lte": to.UTC().Format("2006-01-02"),
			},
		},
		"pageSize": pageSize,
		"page":     page,
	}

	var out listingsResult
	if _, err := c.do(ctx, listingsQuery, vars, &out); err != nil {
		return nil, 0, err
	}
	events := make([]APIEvent, 0, len(out.EventListings.Data))
	for _, row := range out.EventListings.Data {
		events = append(events, row.Event)
	}
	return events, out.EventListings.TotalResults, nil
}

// detailQuery asks only for sale-state fields. Event.tickets is deliberately
// absent: it returns [] for unauthenticated clients, so requesting it would add
// payload and imply a capability we do not have.
const detailQuery = `
query TDEvent($id: ID!) {
  event(id: $id) {
    id title contentUrl isTicketed queueItEnabled cost
    presaleStatus resaleActive status attending interestedCount dateUpdated
  }
}`

type detailResult struct {
	Event *APIEvent `json:"event"`
}

// Event fetches the current sale state of one event.
func (c *Client) Event(ctx context.Context, id string) (APIEvent, json.RawMessage, error) {
	var out detailResult
	raw, err := c.do(ctx, detailQuery, map[string]any{"id": id}, &out)
	if err != nil {
		return APIEvent{}, nil, err
	}
	if out.Event == nil {
		return APIEvent{}, nil, ErrNotFound
	}
	return *out.Event, raw, nil
}

func snippet(b []byte) string {
	const max = 200
	s := strings.TrimSpace(string(b))
	if len(s) > max {
		return s[:max] + "…"
	}
	return s
}
