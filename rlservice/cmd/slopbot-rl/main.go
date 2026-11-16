package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/krezz1e/slopbot/rlservice/internal/api"
	"github.com/krezz1e/slopbot/rlservice/internal/rocketleague"
	"github.com/dank/rlapi"
)

const defaultRefreshTokenFile = "/var/lib/slopbot/rlapi-refresh-token"

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if len(os.Args) > 1 && os.Args[1] == "auth" {
		if err := authenticate(logger); err != nil {
			logger.Error("Rocket League authorization failed", "err", err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] != "serve" {
		fmt.Fprintln(os.Stderr, "usage: slopbot-rl [serve|auth]")
		os.Exit(2)
	}
	if err := serve(logger); err != nil {
		logger.Error("Rocket League service stopped", "err", err)
		os.Exit(1)
	}
}

func serve(logger *slog.Logger) error {
	listenAddress := environment("RLAPI_LISTEN", "127.0.0.1:8765")
	if err := validateLoopbackAddress(listenAddress); err != nil {
		return err
	}
	serviceToken := strings.TrimSpace(os.Getenv("ROCKET_LEAGUE_SERVICE_TOKEN"))
	if len(serviceToken) < 32 {
		return errors.New("ROCKET_LEAGUE_SERVICE_TOKEN must contain at least 32 characters")
	}
	config := rocketleague.Config{
		RefreshTokenFile: environment("RLAPI_REFRESH_TOKEN_FILE", defaultRefreshTokenFile),
		GameVersion:      strings.TrimSpace(os.Getenv("RLAPI_GAME_VERSION")),
		FeatureSet:       strings.TrimSpace(os.Getenv("RLAPI_FEATURE_SET")),
	}
	if (config.GameVersion == "") != (config.FeatureSet == "") {
		return errors.New("RLAPI_GAME_VERSION and RLAPI_FEATURE_SET must be set together")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	client := rocketleague.NewClient(config, logger)
	go client.Run(ctx)

	server := &http.Server{
		Addr:              listenAddress,
		Handler:           api.NewServer(client, serviceToken),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       20 * time.Second,
		WriteTimeout:      20 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	serverErrors := make(chan error, 1)
	go func() {
		logger.Info("Rocket League HTTP service listening", "address", listenAddress)
		serverErrors <- server.ListenAndServe()
	}()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(shutdownCtx)
	case err := <-serverErrors:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

func authenticate(logger *slog.Logger) error {
	path := environment("RLAPI_REFRESH_TOKEN_FILE", defaultRefreshTokenFile)
	egs := rlapi.NewEGS()
	device, err := egs.AuthenticateWithDevice()
	if err != nil {
		return fmt.Errorf("start device authorization: %w", err)
	}
	fmt.Printf("Open %s and enter code: %s\n", device.VerificationURI, device.UserCode)
	token, err := egs.WaitForDeviceAuthorization(device)
	if err != nil {
		return fmt.Errorf("complete device authorization: %w", err)
	}
	if token.RefreshToken == "" {
		return errors.New("Epic did not return a refresh token")
	}
	if err := rocketleague.WriteRefreshToken(path, token.RefreshToken); err != nil {
		return err
	}
	logger.Info("Rocket League authorization saved", "path", path)
	return nil
}

func validateLoopbackAddress(address string) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return fmt.Errorf("RLAPI_LISTEN must be a host:port address: %w", err)
	}
	if strings.EqualFold(host, "localhost") {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return errors.New("RLAPI_LISTEN must use a loopback address")
	}
	return nil
}

func environment(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}
