// Command ingest runs the TicketDrop ingestion service.
//
// EDMTrain discovery, RA enumeration and availability polling, and cross-source
// resolution run as independently supervised loops.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"golang.org/x/sync/errgroup"

	"ticketdrop/ingest/internal/cache"
	"ticketdrop/ingest/internal/config"
	"ticketdrop/ingest/internal/pipeline"
	"ticketdrop/ingest/internal/pool"
	"ticketdrop/ingest/internal/source"
	"ticketdrop/ingest/internal/source/edmtrain"
	"ticketdrop/ingest/internal/source/ra"
	"ticketdrop/ingest/internal/store"
	"ticketdrop/ingest/internal/stream"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: cfg.LogLevel}))
	slog.SetDefault(log)

	ctx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()

	st, err := store.New(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.Close()

	pub, err := stream.New(cfg.RedisURL, cfg.RedisStream, cfg.StreamMaxLen)
	if err != nil {
		return err
	}
	defer pub.Close()
	if err := pub.Ping(ctx); err != nil {
		return err
	}

	log.Info("connected", "stream", cfg.RedisStream)

	snapshots, err := cache.New(cfg.RedisURL)
	if err != nil {
		return err
	}
	defer snapshots.Close()

	srv := startAdminServer(cfg.MetricsAddr, log)
	defer shutdown(srv, log)

	// Each loop runs independently. One tier being disabled or failing must not
	// take the other down: discovery and availability answer different
	// questions and have no runtime dependency on each other.
	group, gctx := errgroup.WithContext(ctx)
	var started int

	if cfg.EDMTrain.Enabled() {
		if len(cfg.EDMTrain.LocationIDs) == 0 {
			log.Warn("EDMTRAIN_LOCATION_IDS is empty; the API will return every upcoming event, " +
				"which is slow and wasteful. Look up ids via the Locations API.")
		}
		client := edmtrain.NewClient(cfg.EDMTrain.BaseURL, cfg.EDMTrain.APIKey, cfg.EDMTrain.RequestTimeout)
		adapter := edmtrain.NewAdapter(client, cfg.EDMTrain.LocationIDs, cfg.EDMTrain.IncludeLivestreams)
		discovery := pipeline.NewDiscovery(adapter, st, pub, log,
			cfg.EDMTrain.PollInterval, cfg.EDMTrain.Lookback)

		group.Go(func() error { return discovery.Run(gctx) })
		started++
	} else {
		// A missing credential disables discovery rather than preventing the RA
		// availability tier from running.
		log.Warn("EDMTRAIN_API_KEY not set, discovery tier disabled")
	}

	if cfg.RA.Enabled() {
		raClient := ra.NewClient(cfg.RA.Endpoint, cfg.RA.UserAgent, cfg.RA.RequestTimeout)
		raAdapter := ra.NewAdapter(raClient, ra.ParseAreaIDs(cfg.RA.AreaIDs), cfg.RA.Lookahead)

		enumerator := pipeline.NewEnumerator(raAdapter, st, log,
			cfg.RA.EnumerateInterval, cfg.RA.EnumeratePageSize)
		group.Go(func() error { return enumerator.Run(gctx) })

		if cfg.RA.SeedWatchlist > 0 {
			group.Go(func() error {
				return seedWatchlist(gctx, st, log, cfg.RA.SeedWatchlist)
			})
		}

		workers := pool.New(cfg.RA.Workers, cfg.RA.QueueSize)
		availability := pipeline.NewAvailability(raAdapter, st, snapshots, pub, workers, log,
			cfg.RA.PollInterval, cfg.RA.BatchSize)
		group.Go(func() error { return availability.Run(gctx) })
		started += 2
	} else {
		log.Warn("RA_AREAS not set, availability tier disabled")
	}

	// Linking only means something when both catalogues are being filled.
	if cfg.EDMTrain.Enabled() && cfg.RA.Enabled() {
		resolver := pipeline.NewResolver(st, log,
			source.PlatformEDMTrain, source.PlatformRA,
			cfg.RA.ResolveInterval, cfg.RA.ResolveBatchSize)
		group.Go(func() error { return resolver.Run(gctx) })
	}

	if started == 0 {
		log.Error("no ingestion tier is configured; set EDMTRAIN_API_KEY or RA_AREAS")
		<-ctx.Done()
		return nil
	}
	return group.Wait()
}

// seedWatchlist is a development convenience so the availability loop has work
// before the harness exists to promote events by hype score. It waits for the
// first enumerate pass to land rather than racing an empty table.
func seedWatchlist(ctx context.Context, st *store.Store, log *slog.Logger, limit int) error {
	select {
	case <-ctx.Done():
		return nil
	case <-time.After(5 * time.Second):
	}
	n, err := st.SeedWatchlist(ctx, source.PlatformRA, limit)
	if err != nil {
		log.Warn("dev watchlist seed failed", "err", err)
		return nil
	}
	log.Warn("dev watchlist seed applied; production promotion is the harness's job",
		"events_added", n)
	return nil
}

// startAdminServer exposes /metrics and /healthz on net/http rather than
// Fiber. The service has no API surface, and promhttp is already a net/http
// handler, so a framework would add a dependency and an adapter for two
// endpoints.
func startAdminServer(addr string, log *slog.Logger) *http.Server {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("admin server failed", "err", err)
		}
	}()
	log.Info("admin server listening", "addr", addr)
	return srv
}

func shutdown(srv *http.Server, log *slog.Logger) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Warn("admin server shutdown", "err", err)
	}
}
