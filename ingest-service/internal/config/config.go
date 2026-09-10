package config

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/joho/godotenv"
)

type Config struct {
	DatabaseURL  string
	RedisURL     string
	RedisStream  string
	StreamMaxLen int64

	EDMTrain EDMTrain
	RA       RA

	MetricsAddr string
	LogLevel    slog.Level
}

type RA struct {
	Endpoint  string
	UserAgent string
	AreaIDs   []string
	Lookahead time.Duration

	// Availability tier: fast loop over watchlisted events only.
	PollInterval   time.Duration
	RequestTimeout time.Duration
	BatchSize      int
	Workers        int
	QueueSize      int

	// Enumerate loop: slow, exists only to learn RA event ids.
	EnumerateInterval time.Duration
	EnumeratePageSize int

	// Cross-source linking. Slow and off the hot path: a missed link costs one
	// unwatched show, so there is nothing to gain from running it often.
	ResolveInterval  time.Duration
	ResolveBatchSize int

	// Development helper, not a production path.
	SeedWatchlist int
}

// Enabled reports whether the availability tier has anything to poll. RA needs
// no credential, so the gate is configuration rather than auth.
func (r RA) Enabled() bool { return len(r.AreaIDs) > 0 }

type EDMTrain struct {
	APIKey             string
	BaseURL            string
	LocationIDs        []string
	PollInterval       time.Duration
	RequestTimeout     time.Duration
	IncludeLivestreams bool

	// How far back each poll reaches beyond the stored watermark. Overlap is
	// deliberately generous: EDMTrain documents createdStartDate as a UTC
	// *date*, so sub-day precision is not available, and re-fetching costs
	// nothing because dedupe is enforced by a unique constraint.
	Lookback time.Duration
}

// Enabled reports whether the discovery tier can run. A missing credential
// disables discovery without taking down availability polling.
func (e EDMTrain) Enabled() bool { return e.APIKey != "" }

func Load() (Config, error) {
	loadDotEnv()

	cfg := Config{
		DatabaseURL:  env("DATABASE_URL", ""),
		RedisURL:     env("REDIS_URL", "redis://localhost:6379/0"),
		RedisStream:  env("REDIS_STREAM", "ticket:drops"),
		StreamMaxLen: int64(envInt("REDIS_STREAM_MAXLEN", 100_000)),
		MetricsAddr:  ":" + env("INGEST_METRICS_PORT", "2112"),
		LogLevel:     parseLevel(env("LOG_LEVEL", "info")),
		EDMTrain: EDMTrain{
			APIKey:             env("EDMTRAIN_API_KEY", ""),
			BaseURL:            strings.TrimRight(env("EDMTRAIN_BASE_URL", "https://edmtrain.com/api"), "/"),
			LocationIDs:        splitCSV(env("EDMTRAIN_LOCATION_IDS", "")),
			PollInterval:       envMillis("EDMTRAIN_POLL_INTERVAL_MS", 60*time.Second),
			RequestTimeout:     envMillis("POLL_TIMEOUT_MS", 10*time.Second),
			IncludeLivestreams: envBool("EDMTRAIN_INCLUDE_LIVESTREAMS", false),
			Lookback:           envMillis("EDMTRAIN_LOOKBACK_MS", 48*time.Hour),
		},
		RA: RA{
			Endpoint:  env("RA_GRAPHQL_URL", "https://ra.co/graphql"),
			UserAgent: env("RA_USER_AGENT", defaultUserAgent),
			AreaIDs:   splitCSV(env("RA_AREAS", "")),
			Lookahead: time.Duration(envInt("RA_LOOKAHEAD_DAYS", 60)) * 24 * time.Hour,

			PollInterval:   envMillis("POLL_INTERVAL_MS", 2*time.Second),
			RequestTimeout: envMillis("POLL_TIMEOUT_MS", 4*time.Second),
			BatchSize:      envInt("RA_BATCH_SIZE", 200),
			// Little's Law: workers ≈ throughput × latency. 200 events per 2 s
			// is 100/sec, and RA responds in roughly 200 ms, so ~20 workers
			// saturates the target rate without piling on upstream concurrency.
			Workers:   envInt("RA_WORKERS", 20),
			QueueSize: envInt("RA_QUEUE_SIZE", 400),

			EnumerateInterval: envMillis("RA_ENUMERATE_INTERVAL_MS", 15*time.Minute),
			EnumeratePageSize: envInt("RA_ENUMERATE_PAGE_SIZE", 100),

			ResolveInterval:  envMillis("RESOLVE_INTERVAL_MS", 5*time.Minute),
			ResolveBatchSize: envInt("RESOLVE_BATCH_SIZE", 100),

			SeedWatchlist: envInt("RA_DEV_SEED_WATCHLIST", 0),
		},
	}

	if cfg.DatabaseURL == "" {
		return Config{}, errors.New("DATABASE_URL is required")
	}
	if cfg.EDMTrain.PollInterval < 10*time.Second {
		// Their terms forbid circumventing rate limits and set them at their
		// discretion. A floor in code is cheaper than an apology email.
		return Config{}, fmt.Errorf("EDMTRAIN_POLL_INTERVAL_MS must be >= 10000, got %s", cfg.EDMTrain.PollInterval)
	}
	if cfg.RA.PollInterval < 500*time.Millisecond {
		// RA publishes no limits, which is a reason for more restraint rather
		// than less: an undocumented limit is enforced by an IP block.
		return Config{}, fmt.Errorf("POLL_INTERVAL_MS must be >= 500, got %s", cfg.RA.PollInterval)
	}
	if cfg.RA.UserAgent == "" {
		// The endpoint rejects requests without a browser User-Agent.
		return Config{}, errors.New("RA_USER_AGENT must not be empty")
	}
	return cfg, nil
}

const defaultUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
	"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

// loadDotEnv walks up from the working directory so the service can be run
// from either the repo root or ingest-service/.
func loadDotEnv() {
	dir, err := os.Getwd()
	if err != nil {
		return
	}
	for i := 0; i < 4; i++ {
		path := filepath.Join(dir, ".env")
		if _, err := os.Stat(path); err == nil {
			_ = godotenv.Load(path)
			return
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return
		}
		dir = parent
	}
}

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v, err := strconv.Atoi(env(key, "")); err == nil {
		return v
	}
	return def
}

func envBool(key string, def bool) bool {
	if v, err := strconv.ParseBool(env(key, "")); err == nil {
		return v
	}
	return def
}

func envMillis(key string, def time.Duration) time.Duration {
	if ms := envInt(key, 0); ms > 0 {
		return time.Duration(ms) * time.Millisecond
	}
	return def
}

func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func parseLevel(s string) slog.Level {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
