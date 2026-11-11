// Command ingest runs the TicketDrop ingestion service.
//
// RA catalogue enumeration and availability polling run as independently
// supervised loops.
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

	srv := startAdminServer(cfg.MetricsAddr, log, func(ctx context.Context) error {
		if err := st.Ping(ctx); err != nil {
			return err
		}
		return pub.Ping(ctx)
	})
	defer shutdown(srv, log)

	group, gctx := errgroup.WithContext(ctx)
	var started int

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
		availability := pipeline.NewAvailability(raAdapter, st, snapshots, workers, log,
			cfg.RA.PollInterval, cfg.RA.BatchSize)
		group.Go(func() error { return availability.Run(gctx) })
		outbox := pipeline.NewOutboxDispatcher(st, pub, log)
		group.Go(func() error { return outbox.Run(gctx) })
		started += 3
	} else {
		log.Warn("RA_AREAS not set, availability tier disabled")
	}

	if started == 0 {
		log.Error("ingestion is not configured; set RA_AREAS")
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
	log.Warn("dev watchlist seed applied; production subscriptions come from the harness",
		"events_added", n)
	return nil
}

// startAdminServer exposes /metrics and /healthz on net/http rather than
// Fiber. The service has no API surface, and promhttp is already a net/http
// handler, so a framework would add a dependency and an adapter for two
// endpoints.
func startAdminServer(
	addr string,
	log *slog.Logger,
	ready func(context.Context) error,
) *http.Server {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := ready(ctx); err != nil {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready"))
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
