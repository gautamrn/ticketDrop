package edmtrain

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestClientEvents(t *testing.T) {
	tests := []struct {
		name       string
		status     int
		body       string
		wantErr    error
		wantErrSub string
		wantCount  int
	}{
		{
			name:      "success returns events",
			status:    http.StatusOK,
			body:      `{"success":true,"message":null,"data":[{"id":1,"name":"A"},{"id":2,"name":"B"}]}`,
			wantCount: 2,
		},
		{
			name:      "success with empty data",
			status:    http.StatusOK,
			body:      `{"success":true,"data":[]}`,
			wantCount: 0,
		},
		{
			name:    "http 429 maps to rate limited",
			status:  http.StatusTooManyRequests,
			body:    `{"success":false,"message":"slow down"}`,
			wantErr: ErrRateLimited,
		},
		{
			name:    "http 401 maps to unauthorized",
			status:  http.StatusUnauthorized,
			body:    `{"success":false,"message":"Invalid client key"}`,
			wantErr: ErrUnauthorized,
		},
		{
			name:    "http 403 maps to unauthorized",
			status:  http.StatusForbidden,
			body:    `{"success":false,"message":"forbidden"}`,
			wantErr: ErrUnauthorized,
		},
		{
			// A 200 carrying success:false is the documented error shape, and
			// a quota message must not be retried like a transient fault.
			name:    "200 with quota message maps to rate limited",
			status:  http.StatusOK,
			body:    `{"success":false,"message":"Daily request limit exceeded"}`,
			wantErr: ErrRateLimited,
		},
		{
			name:       "200 with other api error surfaces message",
			status:     http.StatusOK,
			body:       `{"success":false,"message":"Unknown location"}`,
			wantErrSub: "Unknown location",
		},
		{
			name:       "malformed json is an error, not a panic",
			status:     http.StatusOK,
			body:       `{"success":true,"data":[{`,
			wantErrSub: "decode",
		},
		{
			name:       "unexpected 500",
			status:     http.StatusInternalServerError,
			body:       `oops`,
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

			c := NewClient(srv.URL, "test-key", 5*time.Second)
			got, err := c.Events(context.Background(), EventQuery{})

			switch {
			case tt.wantErr != nil:
				if !errors.Is(err, tt.wantErr) {
					t.Fatalf("want error %v, got %v", tt.wantErr, err)
				}
			case tt.wantErrSub != "":
				if err == nil || !strings.Contains(err.Error(), tt.wantErrSub) {
					t.Fatalf("want error containing %q, got %v", tt.wantErrSub, err)
				}
			default:
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if len(got) != tt.wantCount {
					t.Fatalf("want %d events, got %d", tt.wantCount, len(got))
				}
			}
		})
	}
}

func TestClientQueryParams(t *testing.T) {
	day := time.Date(2026, 8, 9, 17, 45, 0, 0, time.UTC)

	tests := []struct {
		name   string
		query  EventQuery
		want   map[string]string
		absent []string
	}{
		{
			name:   "key is always sent",
			query:  EventQuery{},
			want:   map[string]string{"client": "test-key", "livestreamInd": "false"},
			absent: []string{"locationIds", "createdStartDate", "startDate"},
		},
		{
			name:  "location ids are comma joined",
			query: EventQuery{LocationIDs: []string{"36", "38"}},
			want:  map[string]string{"locationIds": "36,38"},
		},
		{
			// The parameter is documented as a date, so a timestamp must be
			// truncated rather than sent in a format the API may reject.
			name:  "createdStartDate is truncated to a UTC date",
			query: EventQuery{CreatedStartDate: day},
			want:  map[string]string{"createdStartDate": "2026-08-09"},
		},
		{
			name:   "livestreams included omits the filter",
			query:  EventQuery{IncludeLivestreams: true},
			absent: []string{"livestreamInd"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var got url.Values
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				got = r.URL.Query()
				_, _ = w.Write([]byte(`{"success":true,"data":[]}`))
			}))
			defer srv.Close()

			c := NewClient(srv.URL, "test-key", 5*time.Second)
			if _, err := c.Events(context.Background(), tt.query); err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			for k, want := range tt.want {
				if got.Get(k) != want {
					t.Errorf("param %s: want %q, got %q", k, want, got.Get(k))
				}
			}
			for _, k := range tt.absent {
				if got.Has(k) {
					t.Errorf("param %s should be absent, got %q", k, got.Get(k))
				}
			}
		})
	}
}

// The API key travels in the query string, so any error carrying the URL
// leaks it into logs. Transport errors are the path where that happens.
func TestClientRedactsKeyInTransportError(t *testing.T) {
	const key = "super-secret-key"
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	srv.Close() // force a connection failure

	c := NewClient(srv.URL, key, 200*time.Millisecond)
	_, err := c.Events(context.Background(), EventQuery{})
	if err == nil {
		t.Fatal("want a transport error")
	}
	if strings.Contains(err.Error(), key) {
		t.Fatalf("api key leaked into error: %s", err)
	}
	if !strings.Contains(err.Error(), "REDACTED") {
		t.Fatalf("want REDACTED marker, got: %s", err)
	}
}

func TestClientRespectsContextCancellation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(2 * time.Second)
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	c := NewClient(srv.URL, "k", 5*time.Second)
	if _, err := c.Events(ctx, EventQuery{}); err == nil {
		t.Fatal("want context deadline error")
	}
}
