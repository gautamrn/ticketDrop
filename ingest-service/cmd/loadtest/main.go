// Command loadtest drives the real mock-RA -> ingest -> Redis -> harness path.
//
// It must run against docker-compose.load.yml, which is disposable by design.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

type config struct {
	databaseURL string
	redisURL    string
	mockRAURL   string
	emailURL    string
	metricsURL  string
	rates       []int
	duration    time.Duration
	killRate    int
	killDur     time.Duration
	drain       time.Duration
	repo        string
	report      string
	raw         string
	command     string
	renderOnly  bool
}

type phaseResult struct {
	Name                  string  `json:"name"`
	TargetRate            int     `json:"target_rate_eps"`
	DurationSeconds       float64 `json:"duration_seconds"`
	Expected              int     `json:"expected_events"`
	AchievedInputRate     float64 `json:"achieved_input_eps"`
	SustainedOutputRate   float64 `json:"sustained_output_eps"`
	P50MS                 float64 `json:"latency_p50_ms"`
	P95MS                 float64 `json:"latency_p95_ms"`
	P99MS                 float64 `json:"latency_p99_ms"`
	UniqueEmissions       int     `json:"unique_emissions"`
	DuplicateEmissions    int     `json:"duplicate_emissions"`
	DropRows              int     `json:"drop_rows"`
	NotifiedDrops         int     `json:"notified_drops"`
	FailedDrops           int     `json:"failed_drops"`
	DeliveryLimitFailures int     `json:"delivery_limit_failures"`
	DeliveryRows          int     `json:"delivery_rows"`
	SentDeliveries        int     `json:"sent_deliveries"`
	SendingDeliveries     int     `json:"sending_deliveries"`
	AcceptedAlerts        int     `json:"accepted_alerts"`
	DeduplicatedRequests  int     `json:"provider_deduplicated_requests"`
	DuplicateAlerts       int     `json:"duplicate_alerts"`
	MissingAlerts         int     `json:"missing_alerts"`
	PollJobsDropped       float64 `json:"poll_jobs_dropped"`
	PendingAfterDrain     int64   `json:"pending_after_drain"`
	WorkerKilled          bool    `json:"worker_killed"`
	WorkerKillAtSeconds   float64 `json:"worker_kill_at_seconds,omitempty"`
	WorkerRestartMS       int64   `json:"worker_restart_ms,omitempty"`
	FirstMutationUnixMS   int64   `json:"first_mutation_unix_ms"`
	LastEmissionUnixMS    int64   `json:"last_emission_unix_ms"`
	MutationFailures      int     `json:"mutation_failures"`
	TerminalWaitSeconds   float64 `json:"terminal_wait_seconds"`
	BaselineWaitSeconds   float64 `json:"baseline_wait_seconds"`
	SourceCreateWaitSecs  float64 `json:"source_create_wait_seconds"`
}

type providerStats struct {
	Requests             int `json:"requests"`
	AcceptedAlerts       int `json:"accepted_alerts"`
	DeduplicatedRequests int `json:"deduplicated_requests"`
}

type postRunQuery struct {
	NotifiedSent        int `json:"notified_sent"`
	FailedSending       int `json:"failed_sending"`
	FailedSent          int `json:"failed_sent"`
	DeliveryLimitFailed int `json:"delivery_limit_failed"`
}

type mutationResponse struct {
	ID        string    `json:"id"`
	ChangedAt time.Time `json:"changed_at"`
}

type logger struct {
	mu    sync.Mutex
	lines []string
}

func (l *logger) printf(format string, args ...any) {
	line := fmt.Sprintf(format, args...)
	fmt.Println(line)
	l.mu.Lock()
	l.lines = append(l.lines, line)
	l.mu.Unlock()
}

func main() {
	cfg, err := parseConfig()
	if err != nil {
		fatal(err)
	}
	if cfg.renderOnly {
		if err := renderExisting(cfg); err != nil {
			fatal(err)
		}
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	log := &logger{}
	results, metadata, err := run(ctx, cfg, log)
	if err != nil {
		log.printf("ERROR %v", err)
		_ = writeRaw(cfg.raw, log.lines)
		fatal(err)
	}
	if err := writeRaw(cfg.raw, log.lines); err != nil {
		fatal(err)
	}
	if err := writeReport(cfg, metadata, results, log.lines); err != nil {
		fatal(err)
	}
}

func parseConfig() (config, error) {
	var ratesCSV string
	var duration, killDuration, drain time.Duration
	cfg := config{}
	flag.StringVar(&cfg.databaseURL, "database-url",
		"postgres://ticketdrop:ticketdrop@localhost:55432/ticketdrop?sslmode=disable",
		"disposable load-test database")
	flag.StringVar(&cfg.redisURL, "redis-url", "redis://localhost:56379/0", "load-test Redis")
	flag.StringVar(&cfg.mockRAURL, "mock-ra-url", "http://localhost:18098", "mock RA URL")
	flag.StringVar(&cfg.emailURL, "email-url", "http://localhost:18099", "mock email URL")
	flag.StringVar(&cfg.metricsURL, "metrics-url", "http://localhost:12112/metrics", "ingest metrics")
	flag.StringVar(&ratesCSV, "rates", "2,5,10,20,40", "comma-separated source mutations/sec")
	flag.DurationVar(&duration, "duration", 30*time.Second, "duration per normal phase")
	flag.IntVar(&cfg.killRate, "kill-rate", 20, "worker-kill phase mutations/sec")
	flag.DurationVar(&killDuration, "kill-duration", 30*time.Second, "worker-kill phase duration")
	flag.DurationVar(&drain, "drain-timeout", 120*time.Second, "terminal drain deadline")
	flag.StringVar(&cfg.repo, "repo", "..", "repository root")
	flag.StringVar(&cfg.report, "report", "../docs/load-test.md", "generated Markdown report")
	flag.StringVar(&cfg.raw, "raw-output", "../docs/load-test-raw.txt", "raw run output")
	flag.BoolVar(&cfg.renderOnly, "render-existing", false, "render report from existing raw output")
	flag.StringVar(&cfg.command, "command",
		"go run ./cmd/loadtest -repo .. -rates 2,5,10,20,40 -duration 30s "+
			"-kill-rate 20 -kill-duration 30s -drain-timeout 120s "+
			"-report ../docs/load-test.md -raw-output ../docs/load-test-raw.txt",
		"exact reproduction command recorded in the report")
	flag.Parse()
	for _, part := range strings.Split(ratesCSV, ",") {
		rate, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || rate < 1 {
			return config{}, fmt.Errorf("invalid rate %q", part)
		}
		cfg.rates = append(cfg.rates, rate)
	}
	cfg.duration, cfg.killDur, cfg.drain = duration, killDuration, drain
	if duration <= 0 || killDuration <= 0 || drain <= 0 || cfg.killRate < 1 {
		return config{}, errors.New("durations and kill-rate must be positive")
	}
	abs, err := filepath.Abs(cfg.repo)
	if err != nil {
		return config{}, err
	}
	cfg.repo = abs
	return cfg, nil
}

func run(
	ctx context.Context,
	cfg config,
	log *logger,
) ([]phaseResult, map[string]string, error) {
	pool, err := pgxpool.New(ctx, cfg.databaseURL)
	if err != nil {
		return nil, nil, err
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		return nil, nil, fmt.Errorf("postgres: %w", err)
	}
	opts, err := redis.ParseURL(cfg.redisURL)
	if err != nil {
		return nil, nil, err
	}
	rdb := redis.NewClient(opts)
	defer rdb.Close()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, nil, fmt.Errorf("redis: %w", err)
	}

	metadata := hostMetadata(ctx, cfg)
	metaJSON, _ := json.Marshal(metadata)
	log.printf("RUN_METADATA %s", metaJSON)
	userID, err := createBenchmarkUser(ctx, pool)
	if err != nil {
		return nil, metadata, err
	}

	var results []phaseResult
	for _, rate := range cfg.rates {
		name := fmt.Sprintf("rate-%02d", rate)
		result, err := runPhase(ctx, cfg, log, pool, rdb, userID, name, rate, cfg.duration, false)
		if err != nil {
			return results, metadata, err
		}
		results = append(results, result)
		logResult(log, result)
		_ = resetSource(ctx, cfg.mockRAURL, name)
	}
	result, err := runPhase(
		ctx, cfg, log, pool, rdb, userID, "worker-kill", cfg.killRate, cfg.killDur, true,
	)
	if err != nil {
		return results, metadata, err
	}
	results = append(results, result)
	logResult(log, result)
	_ = resetSource(ctx, cfg.mockRAURL, "worker-kill")
	return results, metadata, nil
}

func runPhase(
	ctx context.Context,
	cfg config,
	log *logger,
	pool *pgxpool.Pool,
	rdb *redis.Client,
	userID, name string,
	rate int,
	duration time.Duration,
	killWorker bool,
) (phaseResult, error) {
	expected := int(math.Round(float64(rate) * duration.Seconds()))
	result := phaseResult{
		Name: name, TargetRate: rate, DurationSeconds: duration.Seconds(),
		Expected: expected, WorkerKilled: killWorker,
	}
	log.printf("PHASE_START name=%s rate=%d duration=%s expected=%d kill=%t",
		name, rate, duration, expected, killWorker)

	createStarted := time.Now()
	if err := createSourceEvents(ctx, cfg.mockRAURL, name, expected); err != nil {
		return result, err
	}
	eventIDs, err := waitForEvents(ctx, pool, name, expected, cfg.drain)
	if err != nil {
		return result, err
	}
	result.SourceCreateWaitSecs = time.Since(createStarted).Seconds()
	if err := subscribeEvents(ctx, pool, userID, name); err != nil {
		return result, err
	}
	baselineStarted := time.Now()
	if err := waitForBaselines(ctx, rdb, eventIDs, cfg.drain); err != nil {
		return result, err
	}
	result.BaselineWaitSeconds = time.Since(baselineStarted).Seconds()

	startID := "0-0"
	if info, err := rdb.XInfoStream(ctx, "ticket:drops").Result(); err == nil {
		startID = info.LastGeneratedID
	}
	providerBefore, err := getProviderStats(ctx, cfg.emailURL)
	if err != nil {
		return result, err
	}
	droppedBefore := scrapeMetric(ctx, cfg.metricsURL, "ingest_poll_dropped_total")

	type killResult struct {
		at      time.Time
		elapsed time.Duration
		err     error
	}
	var killDone chan killResult
	phaseStart := time.Now()
	if killWorker {
		killDone = make(chan killResult, 1)
		go func() {
			timer := time.NewTimer(duration / 2)
			defer timer.Stop()
			select {
			case <-ctx.Done():
				killDone <- killResult{err: ctx.Err()}
			case <-timer.C:
				at := time.Now()
				elapsed, err := restartHarness(ctx, cfg.repo)
				killDone <- killResult{at: at, elapsed: elapsed, err: err}
			}
		}()
	}

	changed := make(map[string]time.Time, expected)
	var firstRequest, lastResponse time.Time
	interval := time.Second / time.Duration(rate)
	for i, eventID := range eventIDs {
		target := phaseStart.Add(time.Duration(i) * interval)
		if wait := time.Until(target); wait > 0 {
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return result, ctx.Err()
			case <-timer.C:
			}
		}
		if firstRequest.IsZero() {
			firstRequest = time.Now()
		}
		mutation, err := mutateTicketed(ctx, cfg.mockRAURL, eventID)
		lastResponse = time.Now()
		if err != nil {
			result.MutationFailures++
			log.printf("MUTATION_ERROR phase=%s event=%s error=%q", name, eventID, err)
			continue
		}
		changed[eventID] = mutation.ChangedAt
	}
	if !firstRequest.IsZero() && lastResponse.After(firstRequest) {
		result.AchievedInputRate = float64(len(changed)) / lastResponse.Sub(firstRequest).Seconds()
	}
	if killWorker {
		killed := <-killDone
		if killed.err != nil {
			return result, fmt.Errorf("worker restart: %w", killed.err)
		}
		result.WorkerKillAtSeconds = killed.at.Sub(phaseStart).Seconds()
		result.WorkerRestartMS = killed.elapsed.Milliseconds()
		log.printf("WORKER_RESTART phase=%s at_seconds=%.3f restart_ms=%d",
			name, result.WorkerKillAtSeconds, result.WorkerRestartMS)
	}

	terminalStarted := time.Now()
	emissions, totalMessages, lastEmission, err := waitForEmissions(
		ctx, rdb, startID, name, len(changed), cfg.drain,
	)
	if err != nil {
		return result, err
	}
	if err := waitForTerminal(ctx, pool, name, len(changed), cfg.drain); err != nil {
		return result, err
	}
	result.TerminalWaitSeconds = time.Since(terminalStarted).Seconds()
	result.UniqueEmissions = len(emissions)
	result.DuplicateEmissions = totalMessages - len(emissions)
	result.LastEmissionUnixMS = lastEmission.UnixMilli()

	latencies := make([]float64, 0, len(emissions))
	var firstMutation time.Time
	for eventID, emittedAt := range emissions {
		sourceAt, ok := changed[eventID]
		if !ok {
			continue
		}
		if firstMutation.IsZero() || sourceAt.Before(firstMutation) {
			firstMutation = sourceAt
		}
		latencies = append(latencies, float64(emittedAt.Sub(sourceAt).Microseconds())/1000)
	}
	sort.Float64s(latencies)
	result.FirstMutationUnixMS = firstMutation.UnixMilli()
	result.P50MS = percentile(latencies, 0.50)
	result.P95MS = percentile(latencies, 0.95)
	result.P99MS = percentile(latencies, 0.99)
	if !firstMutation.IsZero() && lastEmission.After(firstMutation) {
		result.SustainedOutputRate =
			float64(len(emissions)) / lastEmission.Sub(firstMutation).Seconds()
	}

	summary, err := durableCounts(ctx, pool, name)
	if err != nil {
		return result, err
	}
	result.DropRows = summary.DropRows
	result.NotifiedDrops = summary.NotifiedDrops
	result.FailedDrops = summary.FailedDrops
	result.DeliveryLimitFailures = summary.DeliveryLimitFailures
	result.DeliveryRows = summary.DeliveryRows
	result.SentDeliveries = summary.SentDeliveries
	result.SendingDeliveries = summary.SendingDeliveries
	providerAfter, err := getProviderStats(ctx, cfg.emailURL)
	if err != nil {
		return result, err
	}
	result.AcceptedAlerts = providerAfter.AcceptedAlerts - providerBefore.AcceptedAlerts
	result.DeduplicatedRequests =
		providerAfter.DeduplicatedRequests - providerBefore.DeduplicatedRequests
	result.DuplicateAlerts = max(0, result.AcceptedAlerts-len(changed))
	result.MissingAlerts = max(0, len(changed)-result.AcceptedAlerts)
	result.PollJobsDropped =
		scrapeMetric(ctx, cfg.metricsURL, "ingest_poll_dropped_total") - droppedBefore
	if pending, err := rdb.XPending(ctx, "ticket:drops", "tdrop-harness").Result(); err == nil {
		result.PendingAfterDrain = pending.Count
	}
	if err := deactivatePhase(ctx, pool, name); err != nil {
		return result, err
	}
	if err := waitForQueueDrain(ctx, cfg.metricsURL, 30*time.Second); err != nil {
		return result, err
	}
	if err := waitForPollQuiescence(ctx, cfg.metricsURL, 30*time.Second); err != nil {
		return result, err
	}
	return result, nil
}

func createBenchmarkUser(ctx context.Context, pool *pgxpool.Pool) (string, error) {
	userID, channelID := uuid.NewString(), uuid.NewString()
	tx, err := pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `
		INSERT INTO users (id, email, name)
		VALUES ($1::uuid, $2, 'Load test user')`,
		userID, "load-"+userID+"@local.invalid"); err != nil {
		return "", err
	}
	if _, err = tx.Exec(ctx, `
		INSERT INTO notification_channels (id, user_id, kind, destination, verified_at)
		VALUES ($3::uuid, $1::uuid, 'email', $2, now())`,
		userID, "load-"+userID+"@local.invalid", channelID); err != nil {
		return "", err
	}
	return userID, tx.Commit(ctx)
}

func createSourceEvents(ctx context.Context, baseURL, prefix string, count int) error {
	url := fmt.Sprintf("%s/admin/load/events?prefix=%s&count=%d", baseURL, prefix, count)
	resp, err := post(ctx, url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("create events returned %d: %s", resp.StatusCode, body)
	}
	return nil
}

func resetSource(ctx context.Context, baseURL, prefix string) error {
	resp, err := post(ctx, fmt.Sprintf("%s/admin/load/reset?prefix=%s", baseURL, prefix))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("reset source returned %d", resp.StatusCode)
	}
	return nil
}

func waitForEvents(
	ctx context.Context,
	pool *pgxpool.Pool,
	prefix string,
	expected int,
	timeout time.Duration,
) ([]string, error) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		rows, err := pool.Query(ctx, `
			SELECT source_event_id FROM events
			 WHERE source_event_id LIKE $1 ORDER BY source_event_id`, prefix+"-%")
		if err != nil {
			return nil, err
		}
		var ids []string
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return nil, err
			}
			ids = append(ids, id)
		}
		rows.Close()
		if len(ids) == expected {
			return ids, nil
		}
		if err := pause(ctx, 100*time.Millisecond); err != nil {
			return nil, err
		}
	}
	return nil, fmt.Errorf("events %s: timed out waiting for %d catalogue rows", prefix, expected)
}

func subscribeEvents(
	ctx context.Context,
	pool *pgxpool.Pool,
	userID, prefix string,
) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `
		INSERT INTO event_subscriptions (user_id, event_id, origin)
		SELECT $1::uuid, id, 'explicit' FROM events WHERE source_event_id LIKE $2
		ON CONFLICT (user_id, event_id) DO NOTHING`,
		userID, prefix+"-%"); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `
		INSERT INTO watchlist (event_id, tier, reason, active)
		SELECT id, 'hot', 'load test', true FROM events WHERE source_event_id LIKE $1
		ON CONFLICT (event_id) DO UPDATE SET active=true, reason='load test'`,
		prefix+"-%"); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func waitForBaselines(
	ctx context.Context,
	rdb *redis.Client,
	eventIDs []string,
	timeout time.Duration,
) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		missing := 0
		for start := 0; start < len(eventIDs); start += 500 {
			end := min(start+500, len(eventIDs))
			keys := make([]string, 0, end-start)
			for _, id := range eventIDs[start:end] {
				keys = append(keys, "snap:ra:"+id)
			}
			values, err := rdb.MGet(ctx, keys...).Result()
			if err != nil {
				return err
			}
			for _, value := range values {
				if value == nil {
					missing++
				}
			}
		}
		if missing == 0 {
			return nil
		}
		if err := pause(ctx, 100*time.Millisecond); err != nil {
			return err
		}
	}
	return fmt.Errorf("timed out waiting for %d baselines", len(eventIDs))
}

func mutateTicketed(ctx context.Context, baseURL, eventID string) (mutationResponse, error) {
	resp, err := post(ctx, fmt.Sprintf("%s/admin/ticketed?id=%s", baseURL, eventID))
	if err != nil {
		return mutationResponse{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return mutationResponse{}, fmt.Errorf("mutation returned %d: %s", resp.StatusCode, body)
	}
	var result mutationResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return mutationResponse{}, err
	}
	if result.ChangedAt.IsZero() {
		return mutationResponse{}, errors.New("mutation response omitted changed_at")
	}
	return result, nil
}

func waitForEmissions(
	ctx context.Context,
	rdb *redis.Client,
	startID, prefix string,
	expected int,
	timeout time.Duration,
) (map[string]time.Time, int, time.Time, error) {
	deadline := time.Now().Add(timeout)
	emissions := make(map[string]time.Time, expected)
	total := 0
	var last time.Time
	for time.Now().Before(deadline) {
		messages, err := rdb.XRange(ctx, "ticket:drops", "("+startID, "+").Result()
		if err != nil {
			return nil, total, last, err
		}
		emissions = make(map[string]time.Time, expected)
		total = 0
		last = time.Time{}
		for _, message := range messages {
			eventID := fmt.Sprint(message.Values["event_id"])
			if !strings.HasPrefix(eventID, prefix+"-") {
				continue
			}
			emitted, err := streamIDTime(message.ID)
			if err != nil {
				return nil, total, last, err
			}
			total++
			if prior, exists := emissions[eventID]; !exists || emitted.Before(prior) {
				emissions[eventID] = emitted
			}
			if emitted.After(last) {
				last = emitted
			}
		}
		if len(emissions) >= expected {
			return emissions, total, last, nil
		}
		if err := pause(ctx, 100*time.Millisecond); err != nil {
			return nil, total, last, err
		}
	}
	return emissions, total, last,
		fmt.Errorf("emissions %s: got %d/%d unique in %s", prefix, len(emissions), expected, timeout)
}

func waitForTerminal(
	ctx context.Context,
	pool *pgxpool.Pool,
	prefix string,
	expected int,
	timeout time.Duration,
) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		var count int
		err := pool.QueryRow(ctx, `
			SELECT count(*) FROM drops d
			JOIN events e ON e.id=d.event_id
			WHERE e.source_event_id LIKE $1 AND d.state IS NOT NULL`, prefix+"-%").Scan(&count)
		if err != nil {
			return err
		}
		if count == expected {
			return nil
		}
		if err := pause(ctx, 100*time.Millisecond); err != nil {
			return err
		}
	}
	return fmt.Errorf("terminal %s: timed out waiting for %d drops", prefix, expected)
}

type durableSummary struct {
	DropRows, NotifiedDrops, FailedDrops, DeliveryLimitFailures int
	DeliveryRows, SentDeliveries, SendingDeliveries             int
}

func durableCounts(
	ctx context.Context,
	pool *pgxpool.Pool,
	prefix string,
) (durableSummary, error) {
	var summary durableSummary
	err := pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM drops d JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1),
			(SELECT count(*) FROM drops d JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1 AND d.state='NOTIFIED'),
			(SELECT count(*) FROM drops d JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1 AND d.state='FAILED'),
			(SELECT count(*) FROM drops d JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1
			    AND d.error='delivery limit exceeded'),
			(SELECT count(*) FROM notification_deliveries nd
			  JOIN drops d ON d.id=nd.drop_id JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1),
			(SELECT count(*) FROM notification_deliveries nd
			  JOIN drops d ON d.id=nd.drop_id JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1 AND nd.status='sent'),
			(SELECT count(*) FROM notification_deliveries nd
			  JOIN drops d ON d.id=nd.drop_id JOIN events e ON e.id=d.event_id
			  WHERE e.source_event_id LIKE $1 AND nd.status='sending')`,
		prefix+"-%").Scan(
		&summary.DropRows,
		&summary.NotifiedDrops,
		&summary.FailedDrops,
		&summary.DeliveryLimitFailures,
		&summary.DeliveryRows,
		&summary.SentDeliveries,
		&summary.SendingDeliveries,
	)
	return summary, err
}

func deactivatePhase(ctx context.Context, pool *pgxpool.Pool, prefix string) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `
		UPDATE event_subscriptions SET active=false
		 WHERE event_id IN (
			SELECT id FROM events WHERE source_event_id LIKE $1
		 )`, prefix+"-%"); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `
		UPDATE watchlist SET active=false
		 WHERE event_id IN (
			SELECT id FROM events WHERE source_event_id LIKE $1
		 )`, prefix+"-%"); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func waitForQueueDrain(ctx context.Context, metricsURL string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if scrapeMetric(ctx, metricsURL, "ingest_pool_queue_depth") == 0 {
			return nil
		}
		if err := pause(ctx, 100*time.Millisecond); err != nil {
			return err
		}
	}
	return fmt.Errorf("ingest worker queue did not drain within %s", timeout)
}

func waitForPollQuiescence(ctx context.Context, metricsURL string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	last := scrapeMetric(ctx, metricsURL, "ingest_poll_total")
	stableSince := time.Now()
	for time.Now().Before(deadline) {
		if err := pause(ctx, 200*time.Millisecond); err != nil {
			return err
		}
		current := scrapeMetric(ctx, metricsURL, "ingest_poll_total")
		if current != last {
			last = current
			stableSince = time.Now()
			continue
		}
		if time.Since(stableSince) >= time.Second {
			return nil
		}
	}
	return fmt.Errorf("ingest polls did not quiesce within %s", timeout)
}

func getProviderStats(ctx context.Context, baseURL string) (providerStats, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/stats", nil)
	if err != nil {
		return providerStats{}, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return providerStats{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return providerStats{}, fmt.Errorf("provider stats returned %d", resp.StatusCode)
	}
	var stats providerStats
	err = json.NewDecoder(resp.Body).Decode(&stats)
	return stats, err
}

func restartHarness(ctx context.Context, repo string) (time.Duration, error) {
	started := time.Now()
	if err := dockerCompose(ctx, repo, "kill", "-s", "SIGKILL", "harness"); err != nil {
		return 0, err
	}
	if err := dockerCompose(ctx, repo, "up", "-d", "harness"); err != nil {
		return 0, err
	}
	return time.Since(started), nil
}

func dockerCompose(ctx context.Context, repo string, args ...string) error {
	base := []string{"compose", "-p", "ticketdrop-load", "-f",
		filepath.Join(repo, "docker-compose.load.yml")}
	cmd := exec.CommandContext(ctx, "docker", append(base, args...)...)
	cmd.Dir = repo
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker %s: %w: %s", strings.Join(args, " "), err, out)
	}
	return nil
}

func scrapeMetric(ctx context.Context, url, name string) float64 {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var total float64
	for _, line := range strings.Split(string(body), "\n") {
		if !strings.HasPrefix(line, name) || strings.HasPrefix(line, name+"_") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) == 2 {
			value, _ := strconv.ParseFloat(fields[1], 64)
			total += value
		}
	}
	return total
}

func streamIDTime(id string) (time.Time, error) {
	part, _, ok := strings.Cut(id, "-")
	if !ok {
		return time.Time{}, fmt.Errorf("invalid Redis stream id %q", id)
	}
	ms, err := strconv.ParseInt(part, 10, 64)
	if err != nil {
		return time.Time{}, err
	}
	return time.UnixMilli(ms).UTC(), nil
}

func percentile(values []float64, p float64) float64 {
	if len(values) == 0 {
		return 0
	}
	index := int(math.Ceil(p*float64(len(values)))) - 1
	index = min(max(index, 0), len(values)-1)
	return values[index]
}

func pause(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func post(ctx context.Context, url string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, nil)
	if err != nil {
		return nil, err
	}
	return http.DefaultClient.Do(req)
}

func logResult(log *logger, result phaseResult) {
	payload, _ := json.Marshal(result)
	log.printf("PHASE_RESULT %s", payload)
}

func hostMetadata(ctx context.Context, cfg config) map[string]string {
	meta := map[string]string{
		"os": runtime.GOOS, "arch": runtime.GOARCH,
		"logical_cpus":     strconv.Itoa(runtime.NumCPU()),
		"go":               runtime.Version(),
		"command":          cfg.command,
		"container_limits": "none",
	}
	if out, err := commandOutput(ctx, "git", "-C", cfg.repo, "rev-parse", "HEAD"); err == nil {
		meta["commit"] = out
		if status, statusErr := commandOutput(
			ctx, "git", "-C", cfg.repo, "status", "--porcelain",
		); statusErr == nil && status != "" {
			meta["commit"] += " (dirty load-test implementation)"
		}
	}
	if out, err := commandOutput(ctx, "docker", "version", "--format", "{{.Server.Version}}"); err == nil {
		meta["docker"] = out
	}
	if runtime.GOOS == "windows" {
		script := `$cpu=(Get-CimInstance Win32_Processor | Select-Object -First 1 -Expand Name);` +
			`$mem=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory;` +
			`Write-Output "$cpu|$mem"`
		if out, err := commandOutput(ctx, "powershell", "-NoProfile", "-Command", script); err == nil {
			cpu, memory, _ := strings.Cut(out, "|")
			meta["cpu"] = strings.TrimSpace(cpu)
			meta["memory_bytes"] = strings.TrimSpace(memory)
		}
	}
	return meta
}

func commandOutput(ctx context.Context, name string, args ...string) (string, error) {
	var stdout bytes.Buffer
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout = &stdout
	err := cmd.Run()
	return strings.TrimSpace(stdout.String()), err
}

func writeRaw(path string, lines []string) error {
	return os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644)
}

func renderExisting(cfg config) error {
	data, err := os.ReadFile(cfg.raw)
	if err != nil {
		return err
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	metadata := map[string]string{}
	var results []phaseResult
	for _, line := range lines {
		switch {
		case strings.HasPrefix(line, "RUN_METADATA "):
			if err := json.Unmarshal(
				[]byte(strings.TrimPrefix(line, "RUN_METADATA ")), &metadata,
			); err != nil {
				return err
			}
		case strings.HasPrefix(line, "PHASE_RESULT "):
			var result phaseResult
			if err := json.Unmarshal(
				[]byte(strings.TrimPrefix(line, "PHASE_RESULT ")), &result,
			); err != nil {
				return err
			}
			results = append(results, result)
		}
	}
	if len(results) == 0 || metadata["commit"] == "" {
		return errors.New("raw output has no metadata or phase results")
	}
	return writeReport(cfg, metadata, results, lines)
}

func writeReport(
	cfg config,
	meta map[string]string,
	results []phaseResult,
	raw []string,
) error {
	var out strings.Builder
	fmt.Fprintf(&out, "# Load test\n\n")
	fmt.Fprintf(&out, "Generated from an executed disposable-stack run at commit `%s`.\n\n", meta["commit"])
	fmt.Fprintf(&out, "## Harness choice\n\n")
	fmt.Fprintf(&out, "A Go harness fits better than k6 because this benchmark must correlate mock-source timestamps with Redis Stream IDs and durable Postgres rows, not just generate HTTP traffic. It reuses the repository's existing pgx and go-redis dependencies and adds no load-test runtime to the deployment.\n\n")
	fmt.Fprintf(&out, "## Scope and clock\n\n")
	fmt.Fprintf(&out, "The source clock is the mock RA `changed_at` timestamp returned when a ticket-state mutation is accepted; the end clock is the matching Redis Stream entry ID timestamp. New catalogue rows are not timed because the first observation is intentionally a silent baseline. `mock-edmtrain` is absent from this RA-only commit, so reviving it would measure a deleted ingestion path.\n\n")
	fmt.Fprintf(&out, "## Executed environment\n\n")
	fmt.Fprintf(&out, "- OS/architecture: `%s/%s`\n", meta["os"], meta["arch"])
	fmt.Fprintf(&out, "- CPU: `%s` (%s logical CPUs)\n", meta["cpu"], meta["logical_cpus"])
	fmt.Fprintf(&out, "- Physical memory: `%s` bytes\n", meta["memory_bytes"])
	fmt.Fprintf(&out, "- Go: `%s`; Docker Engine: `%s`\n", meta["go"], meta["docker"])
	fmt.Fprintf(&out, "- Container limits: `%s`\n", meta["container_limits"])
	startedAt := time.UnixMilli(results[0].FirstMutationUnixMS).UTC()
	endedAt := time.UnixMilli(results[len(results)-1].LastEmissionUnixMS).UTC()
	fmt.Fprintf(&out, "- Measured source window: `%s` to `%s`\n",
		startedAt.Format(time.RFC3339Nano), endedAt.Format(time.RFC3339Nano))
	fmt.Fprintf(&out, "- Command, run from `ingest-service/`:\n\n```powershell\n%s\n```\n\n", cfg.command)
	fmt.Fprintf(&out, "The images were built earlier in this session, then the reported run was reset and started from the repository root with these exact commands:\n\n```powershell\ndocker compose -p ticketdrop-load -f docker-compose.load.yml down -v --remove-orphans\ndocker compose -p ticketdrop-load -f docker-compose.load.yml up -d\n```\n\nFor a first run from a clean checkout, add `--build` to the final command.\n\n")
	fmt.Fprintf(&out, "## Results\n\n")
	fmt.Fprintf(&out, "| Phase | Target events/s | Events | Sustained drops/s | p50 ms | p95 ms | p99 ms | Duplicate stream entries | Duplicate alerts | Missing alerts | Shed poll jobs |\n")
	fmt.Fprintf(&out, "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n")
	for _, r := range results {
		fmt.Fprintf(&out, "| %s | %d | %d | %.2f | %.1f | %.1f | %.1f | %d | %d | %d | %.0f |\n",
			r.Name, r.TargetRate, r.Expected, r.SustainedOutputRate,
			r.P50MS, r.P95MS, r.P99MS, r.DuplicateEmissions,
			r.DuplicateAlerts, r.MissingAlerts, r.PollJobsDropped)
	}
	fmt.Fprintf(&out, "\n“Sustained drops/s” is unique matching stream emissions divided by first-source-mutation to last-emission wall time, so drain time is included. A duplicate alert means the idempotent mock provider accepted more than one alert for an expected source event; repeated HTTP attempts with the same idempotency key are counted separately in the raw output.\n\n")
	fmt.Fprintf(&out, "## Worker-kill result\n\n")
	kill := results[len(results)-1]
	historical, historicalQuery, historicalRaw, hasHistorical := readHistoricalFailure(cfg.report)
	fmt.Fprintf(&out, "The harness was sent `SIGKILL` %.3f seconds into the worker-kill phase and Docker restarted it in %d ms. After drain there were %d pending stream entries, %d durable drops (%d `NOTIFIED`, %d `FAILED`), %d durable delivery rows (%d `sent`, %d still `sending`), %d provider-deduplicated requests, %d duplicate accepted alerts, and %d missing alerts.\n\n",
		kill.WorkerKillAtSeconds, kill.WorkerRestartMS, kill.PendingAfterDrain,
		kill.DropRows, kill.NotifiedDrops, kill.FailedDrops,
		kill.DeliveryRows, kill.SentDeliveries, kill.SendingDeliveries,
		kill.DeduplicatedRequests,
		kill.DuplicateAlerts, kill.MissingAlerts)
	if kill.FailedDrops > 0 || kill.MissingAlerts > 0 {
		fmt.Fprintf(&out, "**The zero-duplicate assertion held, but the application-level at-least-once claim did not.** The worker kill produced %d `delivery limit exceeded` failures and %d provider-missing alerts. In this executed profile, reclaim runs after 1 second, the claim TTL is 5 seconds, and the delivery ceiling is 3; `Worker.handle` checks the delivery ceiling before checking whether another worker's claim is still live. The evidence supports this failure hypothesis: reclaimed entries can reach the ceiling and be dead-lettered before their stale claim expires.\n\n",
			kill.DeliveryLimitFailures, kill.MissingAlerts)
	} else if hasHistorical {
		fmt.Fprintf(&out, "This final run preserved all alerts, but an earlier same-session run of the same 20 events/s kill profile did not: it accepted %d of %d alerts, leaving %d missing with %d duplicate accepted alerts. Its post-run query found %d drops failed by `delivery limit exceeded`: %d had a `sent` delivery and %d remained `sending` (%d other drops were `NOTIFIED` with `sent` delivery). The zero-duplicate assertion held in both runs, but application-level at-least-once delivery is therefore not reliable under this kill timing.\n\n",
			historical.AcceptedAlerts, historical.Expected, historical.MissingAlerts,
			historical.DuplicateAlerts, historicalQuery.DeliveryLimitFailed,
			historicalQuery.FailedSent, historicalQuery.FailedSending,
			historicalQuery.NotifiedSent)
		fmt.Fprintf(&out, "The failure mechanism is supported by the code and run configuration: reclaim runs after 1 second, the claim TTL is 5 seconds, and the delivery ceiling is 3; `Worker.handle` checks the delivery ceiling before checking whether another worker's claim is still live. Depending on kill timing, reclaimed entries can reach the ceiling and be dead-lettered before the stale claim expires.\n\n")
	} else {
		fmt.Fprintf(&out, "Both the zero-duplicate and application-level at-least-once assertions held in this run.\n\n")
	}
	fmt.Fprintf(&out, "## Bottleneck evidence\n\n")
	worst := results[0]
	for _, result := range results[1:] {
		if result.P99MS > worst.P99MS {
			worst = result
		}
	}
	fmt.Fprintf(&out, "The worst observed p99 was %.1f ms in `%s`; that phase sustained %.2f drops/s and shed %.0f poll jobs. With no shedding at the highest executed rate, this run did not locate the throughput ceiling. The p50/p95 shape is consistent with the configured 500 ms poll scheduler being the dominant latency floor; a higher-rate run is required before attributing saturation to Redis, Postgres, or the email fan-out.\n\n",
		worst.P99MS, worst.Name, worst.SustainedOutputRate, worst.PollJobsDropped)
	fmt.Fprintf(&out, "These laptop/mock-provider results are not production capacity and do not include real RA or Resend network latency.\n\n")
	fmt.Fprintf(&out, "## Raw output\n\nThe final run is stored in [`load-test-raw.txt`](./load-test-raw.txt).\n\n```text\n%s\n```\n",
		strings.Join(raw, "\n"))
	if hasHistorical {
		fmt.Fprintf(&out, "\nThe same-session worker-kill failure and its post-run SQL counts are stored in [`load-test-worker-kill-failure-raw.txt`](./load-test-worker-kill-failure-raw.txt).\n\n```text\n%s\n```\n",
			historicalRaw)
	}
	return os.WriteFile(cfg.report, []byte(out.String()), 0o644)
}

func readHistoricalFailure(reportPath string) (phaseResult, postRunQuery, string, bool) {
	path := filepath.Join(filepath.Dir(reportPath), "load-test-worker-kill-failure-raw.txt")
	data, err := os.ReadFile(path)
	if err != nil {
		return phaseResult{}, postRunQuery{}, "", false
	}
	raw := strings.TrimSpace(string(data))
	var phase phaseResult
	var query postRunQuery
	for _, line := range strings.Split(raw, "\n") {
		switch {
		case strings.HasPrefix(line, "PHASE_RESULT "):
			if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "PHASE_RESULT ")), &phase); err != nil {
				return phaseResult{}, postRunQuery{}, "", false
			}
		case strings.HasPrefix(line, "POST_RUN_QUERY "):
			if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "POST_RUN_QUERY ")), &query); err != nil {
				return phaseResult{}, postRunQuery{}, "", false
			}
		}
	}
	return phase, query, raw, phase.Expected > 0 && query.DeliveryLimitFailed > 0
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
