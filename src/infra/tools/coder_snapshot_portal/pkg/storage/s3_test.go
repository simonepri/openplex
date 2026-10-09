// Tests S3 storage operations, SigV4 authentication headers, and error handling.

package storage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
)

func TestS3StoreGetSnapshot(t *testing.T) {
	manifest := &model.SnapshotManifest{
		Schema:          3,
		Selector:        "snap-abc-123",
		Display:         "test manifest",
		LineageToken:    "lin-test",
		IsRoot:          true,
		Timestamp:       1726700000,
		SizeBytes:       1024,
		FilesCount:      12,
		KopiaSnapshotID: "k-abc",
	}
	manifestBytes, _ := json.Marshal(manifest)

	var receivedAuthHeader string
	var receivedAmzDate string
	var receivedContentSha string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedAuthHeader = r.Header.Get("Authorization")
		receivedAmzDate = r.Header.Get("x-amz-date")
		receivedContentSha = r.Header.Get("x-amz-content-sha256")

		expectedPath := "/my-bucket/owners/user-1/snapshots/snap-abc-123.json"
		if r.URL.Path != expectedPath {
			http.NotFound(w, r)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(manifestBytes)
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint:    server.URL,
		Buckets:     []string{"my-bucket"},
		Region:      "us-east-1",
		Credentials: StaticCredentials{AccessKeyID: "AKIAEXAMPLE", SecretAccessKey: "secret-key-test"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()
	result, err := store.GetSnapshot(ctx, "user-1", "snap-abc-123")
	if err != nil {
		t.Fatalf("unexpected error getting snapshot: %v", err)
	}

	if result.Selector != manifest.Selector {
		t.Errorf("expected selector %s, got %s", manifest.Selector, result.Selector)
	}
	if result.Display != manifest.Display {
		t.Errorf("expected display %s, got %s", manifest.Display, result.Display)
	}

	// Verify SigV4 headers
	if !strings.HasPrefix(receivedAuthHeader, "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/") {
		t.Errorf("invalid Authorization header: %s", receivedAuthHeader)
	}
	if !strings.Contains(receivedAuthHeader, "SignedHeaders=host;x-amz-content-sha256;x-amz-date") {
		t.Errorf("Authorization header missing signed headers: %s", receivedAuthHeader)
	}
	if !strings.Contains(receivedAuthHeader, "Signature=") {
		t.Errorf("Authorization header missing signature: %s", receivedAuthHeader)
	}
	if receivedAmzDate == "" {
		t.Errorf("missing x-amz-date header")
	}
	if receivedContentSha != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Errorf("unexpected payload hash for empty GET body: %s", receivedContentSha)
	}
}

func TestS3StoreUnsignedRequests(t *testing.T) {
	manifest := &model.SnapshotManifest{
		Selector:  "snap-unsigned",
		Timestamp: 1000,
	}
	manifestBytes, _ := json.Marshal(manifest)

	var receivedAuthHeader string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedAuthHeader = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(manifestBytes)
	}))
	defer server.Close()

	// No credentials configured
	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"my-bucket"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()
	_, err = store.GetSnapshot(ctx, "user-1", "snap-unsigned")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if receivedAuthHeader != "" {
		t.Errorf("expected empty Authorization header for unsigned store, got: %s", receivedAuthHeader)
	}
}

func TestS3StoreListUserSnapshotsPagination(t *testing.T) {
	snap1 := &model.SnapshotManifest{Selector: "snap-1", Timestamp: 1000}
	snap2 := &model.SnapshotManifest{Selector: "snap-2", Timestamp: 2000}
	snap1Bytes, _ := json.Marshal(snap1)
	snap2Bytes, _ := json.Marshal(snap2)

	var listCalls atomic.Int32

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("list-type") == "2" {
			callNum := listCalls.Add(1)
			w.Header().Set("Content-Type", "application/xml")
			if callNum == 1 {
				// Return page 1 with continuation token
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>my-bucket</Name>
    <Prefix>owners/user-pagination/snapshots/</Prefix>
    <IsTruncated>true</IsTruncated>
    <NextContinuationToken>token-page-2</NextContinuationToken>
    <Contents>
        <Key>owners/user-pagination/snapshots/snap-1.json</Key>
        <Size>100</Size>
    </Contents>
</ListBucketResult>`)
				return
			}
			// Verify continuation token was passed
			if r.URL.Query().Get("continuation-token") != "token-page-2" {
				t.Errorf("expected continuation-token=token-page-2, got %s", r.URL.Query().Get("continuation-token"))
			}
			// Return page 2
			fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>my-bucket</Name>
    <Prefix>owners/user-pagination/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>owners/user-pagination/snapshots/snap-2.json</Key>
        <Size>200</Size>
    </Contents>
</ListBucketResult>`)
			return
		}

		if r.URL.Path == "/my-bucket/owners/user-pagination/snapshots/snap-1.json" {
			w.Write(snap1Bytes)
			return
		}
		if r.URL.Path == "/my-bucket/owners/user-pagination/snapshots/snap-2.json" {
			w.Write(snap2Bytes)
			return
		}

		http.NotFound(w, r)
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"my-bucket"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()
	results, err := store.ListUserSnapshots(ctx, "user-pagination")
	if err != nil {
		t.Fatalf("failed to list snapshots: %v", err)
	}

	if len(results) != 2 {
		t.Fatalf("expected 2 snapshots across pages, got %d", len(results))
	}
}

func TestS3StoreValidationAndSecurity(t *testing.T) {
	store, _ := NewS3Store(S3Config{
		Endpoint: "http://localhost:9000",
		Buckets:  []string{"my-bucket"},
	})
	ctx := context.Background()

	// Path traversal in userID
	invalidUserIDs := []string{
		"../other-user",
		"user/sub",
		"..",
		"user\\bad",
		"",
	}
	for _, uid := range invalidUserIDs {
		_, err := store.ListUserSnapshots(ctx, uid)
		if !errors.Is(err, ErrInvalidUserID) {
			t.Errorf("expected ErrInvalidUserID for %q, got %v", uid, err)
		}
		_, err = store.GetSnapshot(ctx, uid, "snap-1")
		if !errors.Is(err, ErrInvalidUserID) {
			t.Errorf("expected ErrInvalidUserID for %q, got %v", uid, err)
		}
	}

	// Path traversal in selector
	invalidSelectors := []string{
		"../other-snap",
		"snap/sub",
		"..",
		"",
	}
	for _, sel := range invalidSelectors {
		_, err := store.GetSnapshot(ctx, "valid-user", sel)
		if !errors.Is(err, ErrInvalidSelector) {
			t.Errorf("expected ErrInvalidSelector for selector %q, got %v", sel, err)
		}
	}
}

func TestS3StoreErrorHandling(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "missing") {
			http.NotFound(w, r)
			return
		}
		if strings.Contains(r.URL.Path, "forbidden") {
			http.Error(w, "Access Denied", http.StatusForbidden)
			return
		}
		if strings.Contains(r.URL.Path, "corrupt") {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("not valid json"))
			return
		}
	}))
	defer server.Close()

	store, _ := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"my-bucket"},
	})
	ctx := context.Background()

	// 404 Not Found
	_, err := store.GetSnapshot(ctx, "user-1", "missing-snap")
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("expected ErrNotFound for 404, got %v", err)
	}

	// 403 Forbidden
	_, err = store.GetSnapshot(ctx, "user-1", "forbidden-snap")
	if err == nil || !strings.Contains(err.Error(), "403") {
		t.Errorf("expected 403 error, got %v", err)
	}

	// Corrupt JSON
	_, err = store.GetSnapshot(ctx, "user-1", "corrupt-snap")
	if err == nil || !strings.Contains(err.Error(), "JSON") {
		t.Errorf("expected JSON decoding error, got %v", err)
	}
}

func TestS3StoreListNotFoundGraceful(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"nonexistent-bucket"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	results, err := store.ListUserSnapshots(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("expected nil error on 404 bucket, got: %v", err)
	}
	if len(results) != 0 {
		t.Fatalf("expected empty list on 404 bucket, got %d items", len(results))
	}
}

func TestMemoryStore(t *testing.T) {
	store := NewMemoryStore()
	ctx := context.Background()

	snap1 := &model.SnapshotManifest{Selector: "snap-1", Timestamp: 1000}
	snap2 := &model.SnapshotManifest{Selector: "snap-2", Timestamp: 2000}

	// Save
	if err := store.SaveSnapshot(ctx, "u1", snap1); err != nil {
		t.Fatalf("failed to save snap1: %v", err)
	}
	if err := store.SaveSnapshot(ctx, "u1", snap2); err != nil {
		t.Fatalf("failed to save snap2: %v", err)
	}

	// Get
	got, err := store.GetSnapshot(ctx, "u1", "snap-1")
	if err != nil || got.Selector != "snap-1" {
		t.Errorf("unexpected get result: %v, %v", got, err)
	}

	// Get non-existent
	_, err = store.GetSnapshot(ctx, "u1", "snap-none")
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("expected ErrNotFound, got %v", err)
	}

	// List
	list, err := store.ListUserSnapshots(ctx, "u1")
	if err != nil || len(list) != 2 {
		t.Fatalf("expected 2 snapshots, got %d, err %v", len(list), err)
	}
	if list[0].Selector != "snap-2" { // sorted newest first
		t.Errorf("expected snap-2 first, got %s", list[0].Selector)
	}

	// Delete
	if err := store.DeleteSnapshot(ctx, "u1", "snap-1"); err != nil {
		t.Fatalf("failed to delete snap-1: %v", err)
	}
	_, err = store.GetSnapshot(ctx, "u1", "snap-1")
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("expected ErrNotFound after deletion, got %v", err)
	}
}

func TestS3StoreFindsSnapshotsUnderUserBackups(t *testing.T) {
	manifest := &model.SnapshotManifest{
		Schema:          3,
		Selector:        "snap-dev-123",
		Display:         "dev workspace | 2026-09-27",
		LineageToken:    "lin-dev",
		IsRoot:          true,
		Timestamp:       1726700000,
		SizeBytes:       4096,
		FilesCount:      10,
		KopiaSnapshotID: "k-dev-123",
	}
	manifestBytes, _ := json.Marshal(manifest)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("list-type") == "2" {
			prefix := r.URL.Query().Get("prefix")
			w.Header().Set("Content-Type", "application/xml")

			if prefix == "owners/test-user/snapshots/" {
				// Empty root prefix
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>my-bucket</Name>
    <Prefix>owners/test-user/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
</ListBucketResult>`)
				return
			}

			if prefix == "backups/dev/users/test-user/repos/owners/test-user/snapshots/" {
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>my-bucket</Name>
    <Prefix>backups/dev/users/test-user/repos/owners/test-user/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>backups/dev/users/test-user/repos/owners/test-user/snapshots/snap-dev-123.json</Key>
        <Size>512</Size>
    </Contents>
</ListBucketResult>`)
				return
			}

			// Any other prefix returns empty
			fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>my-bucket</Name>
    <IsTruncated>false</IsTruncated>
</ListBucketResult>`)
			return
		}

		if r.URL.Path == "/my-bucket/backups/dev/users/test-user/repos/owners/test-user/snapshots/snap-dev-123.json" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(manifestBytes)
			return
		}

		http.NotFound(w, r)
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"my-bucket"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()

	// ListUserSnapshots should find snap-dev-123 under backups/dev/users/test-user/repos/
	snapshots, err := store.ListUserSnapshots(ctx, "test-user")
	if err != nil {
		t.Fatalf("ListUserSnapshots failed: %v", err)
	}
	if len(snapshots) != 1 {
		t.Fatalf("expected 1 snapshot, got %d", len(snapshots))
	}
	if snapshots[0].Selector != "snap-dev-123" {
		t.Errorf("expected selector snap-dev-123, got %s", snapshots[0].Selector)
	}

	// GetSnapshot should find snap-dev-123
	snap, err := store.GetSnapshot(ctx, "test-user", "snap-dev-123")
	if err != nil {
		t.Fatalf("GetSnapshot failed: %v", err)
	}
	if snap.Selector != "snap-dev-123" {
		t.Errorf("expected selector snap-dev-123, got %s", snap.Selector)
	}
}

func TestS3StoreEmptyBucketsValidation(t *testing.T) {
	_, err := NewS3Store(S3Config{
		Endpoint: "http://localhost:9000",
		Buckets:  nil,
	})
	if err == nil {
		t.Fatal("expected error for nil Buckets, got nil")
	}

	_, err = NewS3Store(S3Config{
		Endpoint: "http://localhost:9000",
		Buckets:  []string{"", "  "},
	})
	if err == nil {
		t.Fatal("expected error for empty Buckets slice, got nil")
	}
}

func TestS3StoreMultiBucketListingAndMerging(t *testing.T) {
	snap1 := &model.SnapshotManifest{
		Schema:          3,
		Selector:        "snap-1",
		Display:         "cell-1 snap 1",
		Timestamp:       1000,
		SizeBytes:       1024,
		KopiaSnapshotID: "k-1",
	}
	snap2 := &model.SnapshotManifest{
		Schema:          3,
		Selector:        "snap-2",
		Display:         "cell-2 snap 2",
		Timestamp:       2000,
		SizeBytes:       2048,
		KopiaSnapshotID: "k-2",
	}
	snapCommonOlder := &model.SnapshotManifest{
		Schema:          3,
		Selector:        "snap-common",
		Display:         "common older",
		Timestamp:       2500,
		SizeBytes:       3000,
		KopiaSnapshotID: "k-c-old",
	}
	snapCommonNewer := &model.SnapshotManifest{
		Schema:          3,
		Selector:        "snap-common",
		Display:         "common newer",
		Timestamp:       3000,
		SizeBytes:       3500,
		KopiaSnapshotID: "k-c-new",
	}

	snap1Bytes, _ := json.Marshal(snap1)
	snap2Bytes, _ := json.Marshal(snap2)
	snapCommonOlderBytes, _ := json.Marshal(snapCommonOlder)
	snapCommonNewerBytes, _ := json.Marshal(snapCommonNewer)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("list-type") == "2" {
			prefix := r.URL.Query().Get("prefix")
			w.Header().Set("Content-Type", "application/xml")

			if strings.HasPrefix(r.URL.Path, "/cell-1-backups/") && prefix == "owners/user-multi/snapshots/" {
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>cell-1-backups</Name>
    <Prefix>owners/user-multi/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>owners/user-multi/snapshots/snap-1.json</Key>
        <Size>100</Size>
    </Contents>
    <Contents>
        <Key>owners/user-multi/snapshots/snap-common.json</Key>
        <Size>200</Size>
    </Contents>
</ListBucketResult>`)
				return
			}

			if strings.HasPrefix(r.URL.Path, "/cell-2-backups/") && prefix == "owners/user-multi/snapshots/" {
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>cell-2-backups</Name>
    <Prefix>owners/user-multi/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>owners/user-multi/snapshots/snap-2.json</Key>
        <Size>150</Size>
    </Contents>
    <Contents>
        <Key>owners/user-multi/snapshots/snap-common.json</Key>
        <Size>250</Size>
    </Contents>
</ListBucketResult>`)
				return
			}

			fmt.Fprint(w, `<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListBucketResult>`)
			return
		}

		switch r.URL.Path {
		case "/cell-1-backups/owners/user-multi/snapshots/snap-1.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snap1Bytes)
		case "/cell-1-backups/owners/user-multi/snapshots/snap-common.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snapCommonNewerBytes)
		case "/cell-2-backups/owners/user-multi/snapshots/snap-2.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snap2Bytes)
		case "/cell-2-backups/owners/user-multi/snapshots/snap-common.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snapCommonOlderBytes)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"cell-1-backups", "cell-2-backups"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()
	results, err := store.ListUserSnapshots(ctx, "user-multi")
	if err != nil {
		t.Fatalf("ListUserSnapshots failed: %v", err)
	}

	if len(results) != 3 {
		t.Fatalf("expected 3 distinct snapshots merged across 2 buckets, got %d", len(results))
	}

	bySelector := make(map[string]*model.SnapshotManifest)
	for _, snap := range results {
		bySelector[snap.Selector] = snap
	}

	if bySelector["snap-1"] == nil || bySelector["snap-2"] == nil || bySelector["snap-common"] == nil {
		t.Fatalf("missing expected selectors in merged results: %+v", bySelector)
	}

	// The newer manifest for snap-common (timestamp 3000) should be retained
	if bySelector["snap-common"].Display != "common newer" {
		t.Errorf("expected newer version 'common newer', got %q", bySelector["snap-common"].Display)
	}
}

func TestS3StoreMultiBucketSelectorLookup(t *testing.T) {
	snapCell1 := &model.SnapshotManifest{
		Schema:   3,
		Selector: "snap-cell1",
		Display:  "from cell 1",
	}
	snapCell2 := &model.SnapshotManifest{
		Schema:   3,
		Selector: "snap-cell2",
		Display:  "from cell 2",
	}

	snapCell1Bytes, _ := json.Marshal(snapCell1)
	snapCell2Bytes, _ := json.Marshal(snapCell2)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/cell-1-backups/owners/user-lookup/snapshots/snap-cell1.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snapCell1Bytes)
		case "/cell-2-backups/owners/user-lookup/snapshots/snap-cell2.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snapCell2Bytes)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"cell-1-backups", "cell-2-backups"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()

	// Find in bucket 1
	got1, err := store.GetSnapshot(ctx, "user-lookup", "snap-cell1")
	if err != nil {
		t.Fatalf("failed to find snap-cell1: %v", err)
	}
	if got1.Display != "from cell 1" {
		t.Errorf("expected display 'from cell 1', got %q", got1.Display)
	}

	// Find in bucket 2
	got2, err := store.GetSnapshot(ctx, "user-lookup", "snap-cell2")
	if err != nil {
		t.Fatalf("failed to find snap-cell2: %v", err)
	}
	if got2.Display != "from cell 2" {
		t.Errorf("expected display 'from cell 2', got %q", got2.Display)
	}

	// Non-existent snapshot across both buckets
	_, err = store.GetSnapshot(ctx, "user-lookup", "snap-nonexistent")
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("expected ErrNotFound for missing snapshot, got %v", err)
	}
}

func TestS3StoreMultiBucketCandidatePrefixes(t *testing.T) {
	snapPrimary := &model.SnapshotManifest{
		Schema:   3,
		Selector: "snap-root-primary",
		Display:  "primary root",
	}
	snapSecondaryBackup := &model.SnapshotManifest{
		Schema:   3,
		Selector: "snap-dev-secondary",
		Display:  "secondary dev backup",
	}

	snapPrimaryBytes, _ := json.Marshal(snapPrimary)
	snapSecondaryBackupBytes, _ := json.Marshal(snapSecondaryBackup)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("list-type") == "2" {
			prefix := r.URL.Query().Get("prefix")
			w.Header().Set("Content-Type", "application/xml")

			if strings.HasPrefix(r.URL.Path, "/cell-primary/") && prefix == "owners/user-cand/snapshots/" {
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>cell-primary</Name>
    <Prefix>owners/user-cand/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>owners/user-cand/snapshots/snap-root-primary.json</Key>
        <Size>100</Size>
    </Contents>
</ListBucketResult>`)
				return
			}

			if strings.HasPrefix(r.URL.Path, "/cell-secondary/") && prefix == "backups/dev/users/user-cand/repos/owners/user-cand/snapshots/" {
				fmt.Fprint(w, `
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>cell-secondary</Name>
    <Prefix>backups/dev/users/user-cand/repos/owners/user-cand/snapshots/</Prefix>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>backups/dev/users/user-cand/repos/owners/user-cand/snapshots/snap-dev-secondary.json</Key>
        <Size>200</Size>
    </Contents>
</ListBucketResult>`)
				return
			}

			fmt.Fprint(w, `<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated></ListBucketResult>`)
			return
		}

		switch r.URL.Path {
		case "/cell-primary/owners/user-cand/snapshots/snap-root-primary.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snapPrimaryBytes)
		case "/cell-secondary/backups/dev/users/user-cand/repos/owners/user-cand/snapshots/snap-dev-secondary.json":
			w.Header().Set("Content-Type", "application/json")
			w.Write(snapSecondaryBackupBytes)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	store, err := NewS3Store(S3Config{
		Endpoint: server.URL,
		Buckets:  []string{"cell-primary", "cell-secondary"},
	})
	if err != nil {
		t.Fatalf("failed to create S3Store: %v", err)
	}

	ctx := context.Background()

	// ListUserSnapshots should discover both root in primary and candidate prefix in secondary
	snapshots, err := store.ListUserSnapshots(ctx, "user-cand")
	if err != nil {
		t.Fatalf("ListUserSnapshots failed: %v", err)
	}
	if len(snapshots) != 2 {
		t.Fatalf("expected 2 snapshots across primary and secondary buckets, got %d", len(snapshots))
	}

	// Lookup candidate snapshot in secondary bucket
	got, err := store.GetSnapshot(ctx, "user-cand", "snap-dev-secondary")
	if err != nil {
		t.Fatalf("GetSnapshot failed: %v", err)
	}
	if got.Display != "secondary dev backup" {
		t.Errorf("expected display 'secondary dev backup', got %q", got.Display)
	}
}
