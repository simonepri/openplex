// Runs the Coder snapshot portal service with OAuth authentication and snapshot storage.

package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/auth"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/server"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/storage"
)

type portalOptions struct {
	port                   string
	coderURL               string
	coderOAuthClientID     string
	coderOAuthClientSecret string
	coderOAuthRedirectURL  string
	sessionSecret          string
	s3Bucket               string
	s3Endpoint             string
	s3KeyPrefix            string
	awsRegion              string
	awsAccessKeyID         string
	awsSecretAccessKey     string
	coderTemplateName      string
	devMode                bool
}

func getEnv(key, fallback string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return fallback
}

func getEnvBool(key string, fallback bool) bool {
	val := strings.ToLower(os.Getenv(key))
	if val == "" {
		return fallback
	}
	return val == "true" || val == "1" || val == "yes" || val == "on"
}

func parseOptions() portalOptions {
	var opts portalOptions

	flag.StringVar(&opts.port, "port", getEnv("PORT", "8080"), "HTTP port to listen on")
	flag.StringVar(&opts.coderURL, "coder-url", getEnv("CODER_URL", "http://localhost:3000"), "Coder deployment base URL")
	flag.StringVar(&opts.coderOAuthClientID, "coder-oauth-client-id", getEnv("CODER_OAUTH_CLIENT_ID", ""), "Coder OAuth2 client ID")
	flag.StringVar(&opts.coderOAuthClientSecret, "coder-oauth-client-secret", getEnv("CODER_OAUTH_CLIENT_SECRET", ""), "Coder OAuth2 client secret")
	flag.StringVar(&opts.coderOAuthRedirectURL, "coder-oauth-redirect-url", getEnv("CODER_OAUTH_REDIRECT_URL", ""), "Coder OAuth2 callback redirect URL")
	flag.StringVar(&opts.sessionSecret, "session-secret", getEnv("SESSION_SECRET", ""), "HMAC secret key for browser sessions (at least 32 bytes)")
	flag.StringVar(&opts.s3Bucket, "s3-bucket", getEnv("S3_BUCKET", ""), "S3 bucket name for snapshot manifests")
	flag.StringVar(&opts.s3Endpoint, "s3-endpoint", getEnv("S3_ENDPOINT", ""), "Optional custom S3 endpoint URL")
	flag.StringVar(&opts.s3KeyPrefix, "s3-key-prefix", getEnv("S3_KEY_PREFIX", ""), "Optional S3 key prefix for snapshot manifests")
	flag.StringVar(&opts.awsRegion, "aws-region", getEnv("AWS_REGION", "us-east-1"), "AWS region for S3 SigV4 signing")
	flag.StringVar(&opts.awsAccessKeyID, "aws-access-key-id", getEnv("AWS_ACCESS_KEY_ID", ""), "AWS access key ID")
	flag.StringVar(&opts.awsSecretAccessKey, "aws-secret-access-key", getEnv("AWS_SECRET_ACCESS_KEY", ""), "AWS secret access key")
	flag.StringVar(&opts.coderTemplateName, "coder-template-name", getEnv("CODER_TEMPLATE_NAME", "dev"), "Coder workspace template name for restores")
	flag.BoolVar(&opts.devMode, "dev-mode", getEnvBool("DEV_MODE", false), "Enable dev mode using in-memory store and mock data")

	flag.Parse()

	if opts.coderOAuthRedirectURL == "" {
		opts.coderOAuthRedirectURL = fmt.Sprintf("http://localhost:%s/oauth/callback", opts.port)
	}

	return opts
}

func newHTTPClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	for _, certPath := range []string{"/etc/local-ca/ca.crt", "/etc/ssl/local-ca/ca.crt"} {
		if caBytes, err := os.ReadFile(certPath); err == nil {
			pool.AppendCertsFromPEM(caBytes)
		}
	}
	transport.TLSClientConfig = &tls.Config{
		RootCAs: pool,
	}
	return &http.Client{
		Transport: transport,
		Timeout:   30 * time.Second,
	}
}

func initStorage(opts portalOptions, httpClient *http.Client) storage.SnapshotStore {
	if opts.devMode {
		memStore := storage.NewMemoryStore()
		seedSampleData(context.Background(), memStore)
		log.Printf("Using in-memory snapshot store with sample data (DEV_MODE=true)")
		return memStore
	}

	if opts.s3Bucket == "" {
		log.Fatalf("S3_BUCKET is required when DEV_MODE is false")
	}

	s3Store, err := storage.NewS3Store(storage.S3Config{
		Endpoint:        opts.s3Endpoint,
		Bucket:          opts.s3Bucket,
		Region:          opts.awsRegion,
		AccessKeyID:     opts.awsAccessKeyID,
		SecretAccessKey: opts.awsSecretAccessKey,
		KeyPrefix:       opts.s3KeyPrefix,
		HTTPClient:      httpClient,
	})
	if err != nil {
		log.Fatalf("Failed to initialize S3Store: %v", err)
	}
	log.Printf("Using S3 snapshot store (bucket=%s, region=%s)", opts.s3Bucket, opts.awsRegion)
	return s3Store
}

func initAuth(opts *portalOptions, httpClient *http.Client) (*auth.SessionManager, *auth.OAuthConfig) {
	if len(opts.sessionSecret) < 32 {
		if opts.devMode {
			log.Printf("DEV_MODE: SESSION_SECRET is under 32 bytes, using default development secret key")
			opts.sessionSecret = "development-mode-session-secret-key-at-least-32-bytes!!"
		} else {
			log.Fatalf("SESSION_SECRET must be at least 32 bytes (got %d bytes)", len(opts.sessionSecret))
		}
	}

	sessionManager := auth.NewSessionManager([]byte(opts.sessionSecret), 24*time.Hour, !opts.devMode)

	var oauthConfig *auth.OAuthConfig
	if opts.coderOAuthClientID != "" && opts.coderOAuthClientSecret != "" {
		oauthConfig = &auth.OAuthConfig{
			CoderURL:     opts.coderURL,
			ClientID:     opts.coderOAuthClientID,
			ClientSecret: opts.coderOAuthClientSecret,
			RedirectURL:  opts.coderOAuthRedirectURL,
			HTTPClient:   httpClient,
		}
	} else if !opts.devMode {
		log.Fatalf("CODER_OAUTH_CLIENT_ID and CODER_OAUTH_CLIENT_SECRET are required in non-dev mode")
	}

	return sessionManager, oauthConfig
}

func main() {
	opts := parseOptions()
	httpClient := newHTTPClient()
	sessionManager, oauthConfig := initAuth(&opts, httpClient)
	store := initStorage(opts, httpClient)

	srv, err := server.NewServer(server.Config{
		CoderURL:          opts.coderURL,
		CoderTemplateName: opts.coderTemplateName,
		OAuthConfig:       oauthConfig,
		SessionManager:    sessionManager,
		Store:             store,
		DevMode:           opts.devMode,
	})
	if err != nil {
		log.Fatalf("Failed to create server: %v", err)
	}

	runServer(opts.port, opts.coderURL, opts.coderTemplateName, opts.devMode, srv)
}

func runServer(port, coderURL, templateName string, devMode bool, handler http.Handler) {
	httpServer := &http.Server{
		Addr:              ":" + port,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("Starting Coder Snapshot Portal on port %s (CODER_URL=%s, TEMPLATE=%s)", port, coderURL, templateName)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP server failed: %v", err)
		}
	}()

	sig := <-stop
	log.Printf("Received signal %v, shutting down gracefully...", sig)

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("HTTP server forced to shutdown: %v", err)
	}
	log.Println("Server exited cleanly")
}

func seedSampleData(ctx context.Context, memStore *storage.MemoryStore) {
	now := time.Now()
	sampleUsers := []string{"dev-user", "user-1", "user-alice-123"}

	for _, user := range sampleUsers {
		_ = memStore.SaveSnapshot(ctx, user, &model.SnapshotManifest{
			Schema:           3,
			Selector:         "snap-main-base-01",
			Display:          "workspace-base-release",
			LineageToken:     "lin-main-dev",
			IsRoot:           true,
			SourceHostDigest: "sha256:host1digest",
			ScopeDigest:      "sha256:scopedigest1",
			Timestamp:        now.Add(-72 * time.Hour).Unix(),
			SizeBytes:        1024 * 1024 * 850, // 850 MiB
			FilesCount:       2100,
			KopiaSnapshotID:  "kopia-snap-001",
			Cell:             "cell-us-east-1a",
			Team:             "core-platform",
		})

		_ = memStore.SaveSnapshot(ctx, user, &model.SnapshotManifest{
			Schema:           3,
			Selector:         "snap-main-feat-02",
			Display:          "auth-service-integrated",
			LineageToken:     "lin-main-dev",
			ParentLineage:    "lin-main-dev",
			ParentSnapshot:   "snap-main-base-01",
			IsRoot:           false,
			SourceHostDigest: "sha256:host1digest",
			ScopeDigest:      "sha256:scopedigest1",
			Timestamp:        now.Add(-24 * time.Hour).Unix(),
			SizeBytes:        1024 * 1024 * 920,
			FilesCount:       2240,
			KopiaSnapshotID:  "kopia-snap-002",
			Cell:             "cell-us-east-1a",
			Team:             "core-platform",
		})

		_ = memStore.SaveSnapshot(ctx, user, &model.SnapshotManifest{
			Schema:           3,
			Selector:         "snap-ui-branch-01",
			Display:          "dag-visualization-exp",
			LineageToken:     "lin-ui-redesign",
			ParentLineage:    "lin-main-dev",
			ParentSnapshot:   "snap-main-feat-02",
			IsRoot:           false,
			SourceHostDigest: "sha256:host1digest",
			ScopeDigest:      "sha256:scopedigest1",
			Timestamp:        now.Add(-6 * time.Hour).Unix(),
			SizeBytes:        1024 * 1024 * 960,
			FilesCount:       2310,
			KopiaSnapshotID:  "kopia-snap-003",
			Cell:             "cell-us-east-1a",
			Team:             "frontend-team",
		})

		_ = memStore.SaveSnapshot(ctx, user, &model.SnapshotManifest{
			Schema:           3,
			Selector:         "snap-main-latest-03",
			Display:          "main-active-ready",
			LineageToken:     "lin-main-dev",
			ParentLineage:    "lin-main-dev",
			ParentSnapshot:   "snap-main-feat-02",
			IsRoot:           false,
			SourceHostDigest: "sha256:host1digest",
			ScopeDigest:      "sha256:scopedigest1",
			Timestamp:        now.Add(-1 * time.Hour).Unix(),
			SizeBytes:        1024 * 1024 * 990,
			FilesCount:       2380,
			KopiaSnapshotID:  "kopia-snap-004",
			Cell:             "cell-us-east-1a",
			Team:             "core-platform",
		})
	}
}
