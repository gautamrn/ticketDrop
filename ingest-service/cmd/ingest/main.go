// Command ingest runs the TicketDrop ingestion service.
//
// Phase 2a: the EDMTrain discovery tier only. The RA availability tier and its
// bounded worker pool land in 2b.
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

	"ticketdrop/ingest/internal/config"
	"ticketdrop/ingest/internal/pipeline"
	"ticketdrop/ingest/internal/source/edmtrain"
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

	srv := startAdminServer(cfg.MetricsAddr, log)
	defer shutdown(srv, log)

	if !cfg.EDMTrain.Enabled() {
		// Matches how DICE is treated: a missing credential disables an
		// adapter rather than crashing the service.
		log.Warn("EDMTRAIN_API_KEY not set, discovery tier disabled; nothing to do")
		<-ctx.Done()
		return nil
	}

	client := edmtrain.NewClient(cfg.EDMTrain.BaseURL, cfg.EDMTrain.APIKey, cfg.EDMTrain.RequestTimeout)
	adapter := edmtrain.NewAdapter(client, cfg.EDMTrain.LocationIDs, cfg.EDMTrain.IncludeLivestreams)

	if len(cfg.EDMTrain.LocationIDs) == 0 {
		log.Warn("EDMTRAIN_LOCATION_IDS is empty; the API will return every upcoming event, " +
			"which is slow and wasteful. Look up ids via the Locations API.")
	}

	discovery := pipeline.NewDiscovery(
		adapter, st, pub, log,
		cfg.EDMTrain.PollInterval, cfg.EDMTrain.Lookback,
	)
	return discovery.Run(ctx)
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
