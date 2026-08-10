// Package edmtrain wraps the EDMTrain listings API.
//
// EDMTrain is the discovery tier: it answers "what shows exist", not "are
// tickets on sale". Its terms set rate limits at their discretion and forbid
// circumventing them, so this client is deliberately sequential with no retry
// storm. See ARCHITECTURE.md §10.1.
package edmtrain

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// ErrRateLimited is returned for 429 and for quota-style API errors. Callers
// must back off rather than retry; the terms forbid working around limits.
var ErrRateLimited = errors.New("edmtrain: rate limited")

// ErrUnauthorized means the key is missing, wrong, or revoked. Retrying cannot
// fix it, so the poller stops instead of hammering.
var ErrUnauthorized = errors.New("edmtrain: unauthorized")

const maxResponseBytes = 32 << 20

type Client struct {
	baseURL string
	apiKey  string
	http    *http.Client
}

func NewClient(baseURL, apiKey string, timeout time.Duration) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		http: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				MaxIdleConns:        10,
				MaxIdleConnsPerHost: 4,
				IdleConnTimeout:     90 * time.Second,
			},
		},
	}
}

// EventQuery mirrors the documented Event Search parameters. Zero values are
// omitted so we always send the narrowest query we can.
type EventQuery struct {
	LocationIDs []string
	StartDate   time.Time
	EndDate     time.Time

	// CreatedStartDate is the change-feed parameter: events *added to
	// EDMTrain* at or after this UTC date. Documented as a date, so the
	// smallest meaningful step is one day.
	CreatedStartDate time.Time

	IncludeLivestreams bool
}

type apiResponse struct {
	Success bool            `json:"success"`
	Message string          `json:"message"`
	Data    []APIEvent      `json:"data"`
	Raw     json.RawMessage `json:"-"`
}

type APIEvent struct {
	ID                 int64       `json:"id"`
	Link               string      `json:"link"`
	TicketLink         string      `json:"ticketLink"`
	Name               string      `json:"name"`
	Ages               string      `json:"ages"`
	FestivalInd        bool        `json:"festivalInd"`
	LivestreamInd      bool        `json:"livestreamInd"`
	ElectronicGenreInd bool        `json:"electronicGenreInd"`
	OtherGenreInd      bool        `json:"otherGenreInd"`
	Date               string      `json:"date"`
	StartTime          string      `json:"startTime"`
	EndTime            string      `json:"endTime"`
	CreatedDate        string      `json:"createdDate"`
	Venue              APIVenue    `json:"venue"`
	ArtistList         []APIArtist `json:"artistList"`

	// raw is the untouched object for this event, carried through to the
	// events.raw jsonb column so an upstream field change is a backfill
	// rather than lost data.
	raw json.RawMessage
}

type APIVenue struct {
	ID        int64    `json:"id"`
	Name      string   `json:"name"`
	Location  string   `json:"location"`
	Address   string   `json:"address"`
	State     string   `json:"state"`
	Latitude  *float64 `json:"latitude"`
	Longitude *float64 `json:"longitude"`
}

type APIArtist struct {
	ID     int64  `json:"id"`
	Name   string `json:"name"`
	Link   string `json:"link"`
	B2BInd bool   `json:"b2bInd"`
}

func (c *Client) Events(ctx context.Context, q EventQuery) ([]APIEvent, error) {
	endpoint := c.baseURL + "/events?" + c.query(q).Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("edmtrain: build request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		// Never wrap the raw error unredacted: the URL carries the API key.
		return nil, fmt.Errorf("edmtrain: request failed: %w", redactErr(err, c.apiKey))
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, fmt.Errorf("edmtrain: read body: %w", err)
	}

	switch {
	case resp.StatusCode == http.StatusTooManyRequests:
		return nil, ErrRateLimited
	case resp.StatusCode == http.StatusUnauthorized, resp.StatusCode == http.StatusForbidden:
		return nil, ErrUnauthorized
	case resp.StatusCode >= 300:
		return nil, fmt.Errorf("edmtrain: unexpected status %d: %s", resp.StatusCode, snippet(body))
	}

	// Decode twice: once for the typed view, once to keep each event's raw
	// object. json.RawMessage on the outer Data field would lose per-event
	// boundaries, and re-marshaling the struct would silently drop any field
	// EDMTrain adds later.
	var parsed apiResponse
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("edmtrain: decode: %w: %s", err, snippet(body))
	}
	if !parsed.Success {
		if isQuotaMessage(parsed.Message) {
			return nil, ErrRateLimited
		}
		return nil, fmt.Errorf("edmtrain: api error: %s", parsed.Message)
	}

	var rawEnvelope struct {
		Data []json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &rawEnvelope); err == nil {
		for i := range parsed.Data {
			if i < len(rawEnvelope.Data) {
				parsed.Data[i].raw = rawEnvelope.Data[i]
			}
		}
	}

	return parsed.Data, nil
}

func (c *Client) query(q EventQuery) url.Values {
	v := url.Values{}
	v.Set("client", c.apiKey)
	if len(q.LocationIDs) > 0 {
		v.Set("locationIds", strings.Join(q.LocationIDs, ","))
	}
	if !q.StartDate.IsZero() {
		v.Set("startDate", q.StartDate.UTC().Format(dateLayout))
	}
	if !q.EndDate.IsZero() {
		v.Set("endDate", q.EndDate.UTC().Format(dateLayout))
	}
	if !q.CreatedStartDate.IsZero() {
		v.Set("createdStartDate", q.CreatedStartDate.UTC().Format(dateLayout))
	}
	if !q.IncludeLivestreams {
		v.Set("livestreamInd", "false")
	}
	return v
}

func isQuotaMessage(msg string) bool {
	m := strings.ToLower(msg)
	return strings.Contains(m, "limit") || strings.Contains(m, "quota") ||
		strings.Contains(m, "too many")
}

// redactErr strips the API key out of anything that might reach a log line.
func redactErr(err error, key string) error {
	if key == "" || err == nil {
		return err
	}
	msg := strings.ReplaceAll(err.Error(), key, "REDACTED")
	return errors.New(msg)
}

func snippet(b []byte) string {
	const max = 200
	s := strings.TrimSpace(string(b))
	if len(s) > max {
		return s[:max] + "…"
	}
	return s
}
