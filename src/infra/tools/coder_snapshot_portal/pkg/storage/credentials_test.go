// Tests container credential retrieval, caching, and use in S3 request signing.

package storage

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// newCredentialsServer serves container credentials that expire after ttl and counts fetches.
// It rejects requests whose Authorization header does not match token.
func newCredentialsServer(t *testing.T, token string, ttl time.Duration, fetches *atomic.Int32) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != token {
			http.Error(w, "invalid token", http.StatusForbidden)
			return
		}
		fetches.Add(1)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"AccessKeyId":     "ASIAEXAMPLE",
			"SecretAccessKey": "container-secret",
			"Token":           "container-session-token",
			"Expiration":      time.Now().Add(ttl).UTC().Format(time.RFC3339),
		})
	}))
	t.Cleanup(server.Close)
	return server
}

func writeTokenFile(t *testing.T, token string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(token+"\n"), 0o600); err != nil {
		t.Fatalf("failed to write token file: %v", err)
	}
	return path
}

func TestS3StoreSignsWithContainerCredentials(t *testing.T) {
	var fetches atomic.Int32
	credsServer := newCredentialsServer(t, "pod-token", time.Hour, &fetches)

	var receivedAuthHeader, receivedSessionToken string
	s3Server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedAuthHeader = r.Header.Get("Authorization")
		receivedSessionToken = r.Header.Get("x-amz-security-token")
		_, _ = w.Write([]byte(`<ListBucketResult></ListBucketResult>`))
	}))
	defer s3Server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: s3Server.URL,
		Buckets:  []string{"my-bucket"},
		Region:   "us-west-2",
		Credentials: &ContainerCredentials{
			URI:        credsServer.URL,
			TokenFile:  writeTokenFile(t, "pod-token"),
			HTTPClient: credsServer.Client(),
		},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	if _, err := store.ListUserSnapshots(context.Background(), "user-1"); err != nil {
		t.Fatalf("unexpected error listing snapshots: %v", err)
	}

	if !strings.HasPrefix(receivedAuthHeader, "AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/") {
		t.Errorf("request not signed with container credentials: %q", receivedAuthHeader)
	}
	if !strings.Contains(receivedAuthHeader, "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token") {
		t.Errorf("session token not signed: %q", receivedAuthHeader)
	}
	if receivedSessionToken != "container-session-token" {
		t.Errorf("expected session token header, got %q", receivedSessionToken)
	}
}

func TestContainerCredentialsRefetchesOnlyNearExpiry(t *testing.T) {
	tests := []struct {
		name        string
		ttl         time.Duration
		wantFetches int32
	}{
		{name: "reuses credentials far from expiry", ttl: time.Hour, wantFetches: 1},
		{name: "refetches credentials inside refresh window", ttl: time.Minute, wantFetches: 2},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var fetches atomic.Int32
			server := newCredentialsServer(t, "pod-token", tt.ttl, &fetches)
			provider := &ContainerCredentials{
				URI:        server.URL,
				TokenFile:  writeTokenFile(t, "pod-token"),
				HTTPClient: server.Client(),
			}

			for range 2 {
				if _, err := provider.Retrieve(context.Background()); err != nil {
					t.Fatalf("unexpected error retrieving credentials: %v", err)
				}
			}

			if got := fetches.Load(); got != tt.wantFetches {
				t.Errorf("expected %d fetches, got %d", tt.wantFetches, got)
			}
		})
	}
}

func TestContainerCredentialsRejectsEndpointError(t *testing.T) {
	var fetches atomic.Int32
	server := newCredentialsServer(t, "pod-token", time.Hour, &fetches)
	provider := &ContainerCredentials{
		URI:        server.URL,
		TokenFile:  writeTokenFile(t, "wrong-token"),
		HTTPClient: server.Client(),
	}

	if _, err := provider.Retrieve(context.Background()); err == nil {
		t.Fatal("expected error for rejected token, got nil")
	}
}
