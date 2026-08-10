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

	MetricsAddr string
	LogLevel    slog.Level
}

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

// Enabled reports whether the discovery tier can run. Mirrors how DICE is
// treated: a missing credential disables an adapter, it does not crash the
// service.
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
	}

	if cfg.DatabaseURL == "" {
		return Config{}, errors.New("DATABASE_URL is required")
	}
	if cfg.EDMTrain.PollInterval < 10*time.Second {
		// Their terms forbid circumventing rate limits and set them at their
		// discretion. A floor in code is cheaper than an apology email.
		return Config{}, fmt.Errorf("EDMTRAIN_POLL_INTERVAL_MS must be >= 10000, got %s", cfg.EDMTrain.PollInterval)
	}
	return cfg, nil
}

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
