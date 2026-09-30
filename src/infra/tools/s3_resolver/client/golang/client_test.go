// Tests Go S3 resolver client caching, HTTP request resolution, and error handling.

package s3resolver

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

const testURI = "s3://cell-aws-usw2/home/team-a/data.parquet"

func TestClientResolveDirect(t *testing.T) {
	var requestCount int32
	var receivedAuth string

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&requestCount, 1)
		receivedAuth = r.Header.Get("Authorization")

		if r.URL.Query().Get("uri") == testURI {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"mode":         "direct",
				"uri":          testURI,
				"bucket":       "prod-cell-aws-usw2-home",
				"key":          "home/team-a/data.parquet",
				"endpoint_url": "https://s3.us-west-2.amazonaws.com",
				"region":       "us-west-2",
				"auth":         map[string]string{"type": "ambient_workload_identity"},
			})
			return
		}
		w.WriteHeader(http.StatusForbidden)
	}))
	defer ts.Close()

	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-jwt"), 0600); err != nil {
		t.Fatal(err)
	}

	client := NewClient(Options{
		ResolverURL: ts.URL,
		TokenPath:   tokenFile,
		Timeout:     time.Second,
	})

	ctx := context.Background()
	target, err := client.Resolve(ctx, testURI)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if target.Mode != "direct" || !target.IsDirect() {
		t.Errorf("expected direct mode, got %s", target.Mode)
	}
	if target.Bucket != "prod-cell-aws-usw2-home" || target.Key != "home/team-a/data.parquet" {
		t.Errorf("unexpected target coordinates: %s/%s", target.Bucket, target.Key)
	}
	if target.CleanEndpoint() != "s3.us-west-2.amazonaws.com" || target.Scheme() != "https" {
		t.Errorf("unexpected endpoint/scheme: %s (%s)", target.CleanEndpoint(), target.Scheme())
	}
	if target.Region != "us-west-2" || target.AuthType != "ambient_workload_identity" {
		t.Errorf("unexpected region/auth: %s / %s", target.Region, target.AuthType)
	}
	if receivedAuth != "Bearer test-jwt" {
		t.Errorf("expected Bearer test-jwt, got %s", receivedAuth)
	}

	if _, err := client.Resolve(ctx, testURI); err != nil || atomic.LoadInt32(&requestCount) != 1 {
		t.Errorf("expected cached call to not trigger request, got count %d, err %v", requestCount, err)
	}
}

func TestClientFallback(t *testing.T) {
	client := NewClient(Options{
		ResolverURL: "http://127.0.0.1:1/resolve",
		Timeout:     50 * time.Millisecond,
	})
	target, err := client.Resolve(context.Background(), "s3://cell-aws-usw2/scratch/team-a/temp.bin")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if target.Mode != "fallback" || target.IsDirect() {
		t.Errorf("expected fallback mode, got %s", target.Mode)
	}
	if target.Bucket != "cell-aws-usw2" || target.Key != "scratch/team-a/temp.bin" {
		t.Errorf("unexpected bucket/key: %s/%s", target.Bucket, target.Key)
	}
	if target.EndpointURL != "http://s3-gateway.s3-system.svc:10080" {
		t.Errorf("unexpected fallback endpoint: %s", target.EndpointURL)
	}
	if DefaultResolverURL != "http://s3-gateway.s3-system.svc:10080/resolve" {
		t.Errorf("unexpected DefaultResolverURL: %s", DefaultResolverURL)
	}
}

func TestClientInvalidURI(t *testing.T) {
	if _, err := NewClient().Resolve(context.Background(), "https://invalid/scheme"); err == nil {
		t.Errorf("expected error for invalid uri scheme")
	}
}

func TestClientAuthorizationError(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden} {
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(status)
		}))
		defer ts.Close()

		client := NewClient(Options{
			ResolverURL: ts.URL,
			Timeout:     time.Second,
		})

		_, err := client.Resolve(context.Background(), testURI)
		if err == nil {
			t.Fatalf("expected error for status %d, got nil", status)
		}
		expectedMsg := "s3 resolver authorization failed"
		if !strings.Contains(err.Error(), expectedMsg) {
			t.Errorf("expected error containing %q, got %q", expectedMsg, err.Error())
		}
	}
}

