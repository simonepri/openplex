// Tests HTTP routing, authentication middleware, and HTML template rendering.

package server

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/auth"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/storage"
)

type testRig struct {
	server         *Server
	portalServer   *httptest.Server
	mockCoder      *httptest.Server
	memStore       *storage.MemoryStore
	sessionManager *auth.SessionManager
	client         *http.Client
}

func setupTestRig(t *testing.T) *testRig {
	t.Helper()

	// 1. Mock Coder Server
	mockCoder := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/oauth2/tokens":
			_ = r.ParseForm()
			if r.Form.Get("code_verifier") == "" {
				http.Error(w, "missing code_verifier", http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]string{
				"access_token": "mock-access-token-12345",
			})
		case "/api/v2/users/me":
			token := r.Header.Get("Coder-Session-Token")
			if token == "" {
				authHdr := r.Header.Get("Authorization")
				token = strings.TrimPrefix(authHdr, "Bearer ")
			}
			if token != "mock-access-token-12345" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id":       "user-alice-123",
				"username": "alice",
				"email":    "alice@example.com",
				"roles":    []string{"member"},
			})
		default:
			http.NotFound(w, r)
		}
	}))

	// 2. Storage & Seed Data
	memStore := storage.NewMemoryStore()
	ctx := context.Background()

	rootSnap := &model.SnapshotManifest{
		Schema:           3,
		Selector:         "snap-root-01",
		Display:          "root-baseline",
		LineageToken:     "lin-main",
		IsRoot:           true,
		SourceHostDigest: "sha256:host1",
		ScopeDigest:      "sha256:scope1",
		Timestamp:        time.Now().Add(-48 * time.Hour).Unix(),
		SizeBytes:        1024 * 1024 * 500, // 500 MiB
		FilesCount:       1200,
		KopiaSnapshotID:  "k-snap-01",
		Cell:             "cell-us-east-1a",
		Team:             "platform",
	}
	if err := memStore.SaveSnapshot(ctx, "user-alice-123", rootSnap); err != nil {
		t.Fatalf("failed to seed root snapshot: %v", err)
	}

	childSnap := &model.SnapshotManifest{
		Schema:           3,
		Selector:         "snap-child-02",
		Display:          "child-feature",
		LineageToken:     "lin-main",
		ParentLineage:    "lin-main",
		ParentSnapshot:   "snap-root-01",
		IsRoot:           false,
		SourceHostDigest: "sha256:host1",
		ScopeDigest:      "sha256:scope1",
		Timestamp:        time.Now().Add(-2 * time.Hour).Unix(),
		SizeBytes:        1024 * 1024 * 650, // 650 MiB
		FilesCount:       1350,
		KopiaSnapshotID:  "k-snap-02",
		Cell:             "cell-us-east-1a",
		Team:             "platform",
	}
	if err := memStore.SaveSnapshot(ctx, "user-alice-123", childSnap); err != nil {
		t.Fatalf("failed to seed child snapshot: %v", err)
	}

	forkSnap := &model.SnapshotManifest{
		Schema:           3,
		Selector:         "snap-fork-03",
		Display:          "branch-experiment",
		LineageToken:     "lin-branch-exp",
		ParentLineage:    "lin-main",
		ParentSnapshot:   "snap-root-01",
		IsRoot:           false,
		SourceHostDigest: "sha256:host1",
		ScopeDigest:      "sha256:scope1",
		Timestamp:        time.Now().Add(-1 * time.Hour).Unix(),
		SizeBytes:        1024 * 1024 * 510,
		FilesCount:       1250,
		KopiaSnapshotID:  "k-snap-03",
		Cell:             "cell-us-east-1a",
		Team:             "platform",
	}
	if err := memStore.SaveSnapshot(ctx, "user-alice-123", forkSnap); err != nil {
		t.Fatalf("failed to seed fork snapshot: %v", err)
	}

	// 3. Auth & Session Manager
	sessionSecret := []byte("secret-key-at-least-32-bytes-long-for-testing!!")
	sessionManager := auth.NewSessionManager(sessionSecret, 24*time.Hour, false)

	oauthConfig := &auth.OAuthConfig{
		CoderURL:     mockCoder.URL,
		ClientID:     "portal-client-id",
		ClientSecret: "portal-client-secret",
		RedirectURL:  "http://localhost:8080/oauth/callback",
		HTTPClient:   mockCoder.Client(),
	}

	// 4. Portal Server
	srv, err := NewServer(Config{
		CoderURL:          mockCoder.URL,
		CoderTemplateName: "dev",
		OAuthConfig:       oauthConfig,
		SessionManager:    sessionManager,
		Store:             memStore,
		DevMode:           false,
	})
	if err != nil {
		t.Fatalf("failed to initialize Server: %v", err)
	}

	portalServer := httptest.NewServer(srv)

	// Non-redirecting HTTP client to inspect 302 redirects
	client := &http.Client{
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
		Timeout: 5 * time.Second,
	}

	t.Cleanup(func() {
		portalServer.Close()
		mockCoder.Close()
	})

	return &testRig{
		server:         srv,
		portalServer:   portalServer,
		mockCoder:      mockCoder,
		memStore:       memStore,
		sessionManager: sessionManager,
		client:         client,
	}
}

func TestUnauthenticatedRedirect(t *testing.T) {
	rig := setupTestRig(t)

	testCases := []struct {
		name string
		path string
	}{
		{"Dashboard", "/"},
		{"Restore", "/restore?selector=snap-root-01"},
		{"API Snapshots", "/api/v1/snapshots"},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := rig.client.Get(rig.portalServer.URL + tc.path)
			if err != nil {
				t.Fatalf("GET %s failed: %v", tc.path, err)
			}
			defer resp.Body.Close()

			if resp.StatusCode != http.StatusFound {
				t.Fatalf("expected status 302, got %d", resp.StatusCode)
			}

			loc := resp.Header.Get("Location")
			if loc != "/oauth/login" {
				t.Fatalf("expected redirect to /oauth/login, got %q", loc)
			}
		})
	}
}

func TestHealthzAndLivez(t *testing.T) {
	rig := setupTestRig(t)

	endpoints := []string{"/healthz", "/livez"}
	for _, ep := range endpoints {
		t.Run(ep, func(t *testing.T) {
			resp, err := rig.client.Get(rig.portalServer.URL + ep)
			if err != nil {
				t.Fatalf("GET %s failed: %v", ep, err)
			}
			defer resp.Body.Close()

			if resp.StatusCode != http.StatusOK {
				t.Fatalf("expected status 200, got %d", resp.StatusCode)
			}

			body, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatalf("failed to read response: %v", err)
			}

			if !strings.Contains(string(body), "ok") {
				t.Fatalf("expected body to contain 'ok', got %q", string(body))
			}
		})
	}
}

func TestAuthenticatedDashboard(t *testing.T) {
	rig := setupTestRig(t)

	sess := &auth.Session{
		UserID:    "user-alice-123",
		Username:  "alice",
		Email:     "alice@example.com",
		ExpiresAt: time.Now().Add(24 * time.Hour),
	}
	cookie, err := rig.sessionManager.CreateCookie(sess)
	if err != nil {
		t.Fatalf("failed to create session cookie: %v", err)
	}

	req, err := http.NewRequest(http.MethodGet, rig.portalServer.URL+"/", nil)
	if err != nil {
		t.Fatalf("failed to create request: %v", err)
	}
	req.AddCookie(cookie)

	resp, err := rig.client.Do(req)
	if err != nil {
		t.Fatalf("GET / failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("failed to read body: %v", err)
	}
	body := string(bodyBytes)

	// Verify user profile rendered
	if !strings.Contains(body, "alice") {
		t.Errorf("expected dashboard body to contain username 'alice'")
	}
	if !strings.Contains(body, "alice@example.com") {
		t.Errorf("expected dashboard body to contain email 'alice@example.com'")
	}

	// Verify snapshots rendered in timeline and DAG graph
	if !strings.Contains(body, "snap-root-01") {
		t.Errorf("expected dashboard body to contain 'snap-root-01'")
	}
	if !strings.Contains(body, "snap-child-02") {
		t.Errorf("expected dashboard body to contain 'snap-child-02'")
	}
	if !strings.Contains(body, "snap-fork-03") {
		t.Errorf("expected dashboard body to contain 'snap-fork-03'")
	}

	// Verify SVG DAG rendered
	if !strings.Contains(body, "<svg class=\"dag-svg\"") {
		t.Errorf("expected dashboard body to contain SVG DAG element")
	}
	if !strings.Contains(body, "dag-edge") {
		t.Errorf("expected dashboard body to contain dag-edge elements")
	}

	// Verify Use Snapshot action for snap-root-01
	if !strings.Contains(body, "openSelectorModal('snap-root-01'") {
		t.Errorf("expected dashboard body to contain snapshot action for snap-root-01")
	}
}

func TestAuthenticatedDashboardWithWorkspaceContext(t *testing.T) {
	rig := setupTestRig(t)

	sess := &auth.Session{
		UserID:    "user-alice-123",
		Username:  "alice",
		Email:     "alice@example.com",
		ExpiresAt: time.Now().Add(24 * time.Hour),
	}
	cookie, err := rig.sessionManager.CreateCookie(sess)
	if err != nil {
		t.Fatalf("failed to create session cookie: %v", err)
	}

	req, err := http.NewRequest(http.MethodGet, rig.portalServer.URL+"/?owner=alice&workspace=unicorn-2", nil)
	if err != nil {
		t.Fatalf("failed to create request: %v", err)
	}
	req.AddCookie(cookie)

	resp, err := rig.client.Do(req)
	if err != nil {
		t.Fatalf("GET /?owner=alice&workspace=unicorn-2 failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("failed to read body: %v", err)
	}
	body := string(bodyBytes)

	// Verify contextual header
	if !strings.Contains(body, "Restoring into <strong>unicorn-2</strong>") {
		t.Errorf("expected dashboard body to contain contextual restoration lede")
	}

	// Verify contextual return link to parameters
	expectedParamsPath := "/@alice/unicorn-2/settings/parameters"
	if !strings.Contains(body, expectedParamsPath) {
		t.Errorf("expected dashboard body to contain parameters link %q", expectedParamsPath)
	}

	// Verify modal button directs to workspace parameters
	if !strings.Contains(body, "Go to unicorn-2 Parameters &nearr;") {
		t.Errorf("expected modal footer to include 'Go to unicorn-2 Parameters ↗' button")
	}
}

func TestOAuthCallbackRejectsMissingBrowserBinding(t *testing.T) {
	for _, tc := range []struct {
		name, state, cookieState, verifier string
	}{
		{"missing state cookie", "expected-state", "", "verifier"},
		{"missing state parameter", "", "expected-state", "verifier"},
		{"mismatched state", "other-state", "expected-state", "verifier"},
		{"missing verifier", "expected-state", "expected-state", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rig := setupTestRig(t)
			req, err := http.NewRequest(http.MethodGet, rig.portalServer.URL+"/oauth/callback?code=valid-auth-code&state="+tc.state, nil)
			if err != nil {
				t.Fatal(err)
			}
			if tc.cookieState != "" {
				req.AddCookie(&http.Cookie{Name: "coder_oauth_state", Value: tc.cookieState})
			}
			if tc.verifier != "" {
				req.AddCookie(&http.Cookie{Name: "coder_oauth_verifier", Value: tc.verifier})
			}
			resp, err := rig.client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusBadRequest {
				t.Fatalf("unbound callback returned status %d, want 400", resp.StatusCode)
			}
			for _, cookie := range resp.Cookies() {
				if cookie.Name == "coder_snapshot_session" && cookie.Value != "" {
					t.Fatal("unbound callback created a session")
				}
			}
		})
	}
}

func TestOAuthLoginAndCallback(t *testing.T) {
	rig := setupTestRig(t)

	// Step 1: GET /oauth/login
	loginResp, err := rig.client.Get(rig.portalServer.URL + "/oauth/login")
	if err != nil {
		t.Fatalf("GET /oauth/login failed: %v", err)
	}
	defer loginResp.Body.Close()

	if loginResp.StatusCode != http.StatusFound {
		t.Fatalf("expected status 302 on login, got %d", loginResp.StatusCode)
	}

	authURL := loginResp.Header.Get("Location")
	if !strings.Contains(authURL, "/oauth2/authorize") || !strings.Contains(authURL, "client_id=portal-client-id") || !strings.Contains(authURL, "code_challenge=") {
		t.Fatalf("unexpected auth URL: %s", authURL)
	}

	// Extract state and verifier cookies
	var stateCookie *http.Cookie
	var verifierCookie *http.Cookie
	for _, c := range loginResp.Cookies() {
		if c.Name == "coder_oauth_state" {
			stateCookie = c
		}
		if c.Name == "coder_oauth_verifier" {
			verifierCookie = c
		}
	}
	if stateCookie == nil {
		t.Fatalf("expected coder_oauth_state cookie to be set")
	}
	if verifierCookie == nil {
		t.Fatalf("expected coder_oauth_verifier cookie to be set")
	}

	// Step 2: GET /oauth/callback?code=mock-code&state=...
	callbackReq, err := http.NewRequest(
		http.MethodGet,
		rig.portalServer.URL+"/oauth/callback?code=valid-auth-code&state="+stateCookie.Value,
		nil,
	)
	if err != nil {
		t.Fatalf("failed to create callback request: %v", err)
	}
	for _, c := range loginResp.Cookies() {
		callbackReq.AddCookie(c)
	}

	callbackResp, err := rig.client.Do(callbackReq)
	if err != nil {
		t.Fatalf("GET /oauth/callback failed: %v", err)
	}
	defer callbackResp.Body.Close()

	if callbackResp.StatusCode != http.StatusFound {
		t.Fatalf("expected status 302 on callback, got %d", callbackResp.StatusCode)
	}

	if loc := callbackResp.Header.Get("Location"); loc != "/" {
		t.Fatalf("expected redirect to '/', got %q", loc)
	}

	// Verify session cookie was set
	var sessionCookie *http.Cookie
	for _, c := range callbackResp.Cookies() {
		if c.Name == rig.sessionManager.CookieName {
			sessionCookie = c
			break
		}
	}
	if sessionCookie == nil {
		t.Fatalf("expected session cookie %q in response", rig.sessionManager.CookieName)
	}

	// Verify session content
	dummyReq, _ := http.NewRequest(http.MethodGet, "/", nil)
	dummyReq.AddCookie(sessionCookie)
	parsedSession, err := rig.sessionManager.GetSession(dummyReq)
	if err != nil {
		t.Fatalf("failed to parse issued session cookie: %v", err)
	}

	if parsedSession.UserID != "user-alice-123" {
		t.Errorf("expected session UserID 'user-alice-123', got %q", parsedSession.UserID)
	}
	if parsedSession.Username != "alice" {
		t.Errorf("expected session Username 'alice', got %q", parsedSession.Username)
	}
	if parsedSession.Email != "alice@example.com" {
		t.Errorf("expected session Email 'alice@example.com', got %q", parsedSession.Email)
	}
}

func TestRestoreRedirect(t *testing.T) {
	rig := setupTestRig(t)

	sess := &auth.Session{
		UserID:    "user-alice-123",
		Username:  "alice",
		Email:     "alice@example.com",
		ExpiresAt: time.Now().Add(time.Hour),
	}
	cookie, _ := rig.sessionManager.CreateCookie(sess)

	req, _ := http.NewRequest(http.MethodGet, rig.portalServer.URL+"/restore?selector=snap-child-02", nil)
	req.AddCookie(cookie)

	resp, err := rig.client.Do(req)
	if err != nil {
		t.Fatalf("GET /restore failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusFound {
		t.Fatalf("expected status 302, got %d", resp.StatusCode)
	}

	loc := resp.Header.Get("Location")
	expectedPrefix := rig.mockCoder.URL + "/templates/dev/workspace?param.restore_selector=snap-child-02"
	if loc != expectedPrefix {
		t.Fatalf("expected redirect %q, got %q", expectedPrefix, loc)
	}
}

func TestAPISnapshots(t *testing.T) {
	rig := setupTestRig(t)

	sess := &auth.Session{
		UserID:    "user-alice-123",
		Username:  "alice",
		Email:     "alice@example.com",
		ExpiresAt: time.Now().Add(time.Hour),
	}
	cookie, _ := rig.sessionManager.CreateCookie(sess)

	req, _ := http.NewRequest(http.MethodGet, rig.portalServer.URL+"/api/v1/snapshots", nil)
	req.AddCookie(cookie)

	resp, err := rig.client.Do(req)
	if err != nil {
		t.Fatalf("GET /api/v1/snapshots failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	contentType := resp.Header.Get("Content-Type")
	if !strings.Contains(contentType, "application/json") {
		t.Errorf("expected Content-Type application/json, got %q", contentType)
	}

	var snapshots []*model.SnapshotManifest
	if err := json.NewDecoder(resp.Body).Decode(&snapshots); err != nil {
		t.Fatalf("failed to decode JSON response: %v", err)
	}

	if len(snapshots) != 3 {
		t.Fatalf("expected 3 snapshots, got %d", len(snapshots))
	}

	// Verify selectors are present
	selectors := map[string]bool{}
	for _, s := range snapshots {
		selectors[s.Selector] = true
	}
	if !selectors["snap-root-01"] || !selectors["snap-child-02"] || !selectors["snap-fork-03"] {
		t.Errorf("expected selectors snap-root-01, snap-child-02, snap-fork-03, got %v", selectors)
	}
}

func TestLogout(t *testing.T) {
	rig := setupTestRig(t)

	resp, err := rig.client.Get(rig.portalServer.URL + "/logout")
	if err != nil {
		t.Fatalf("GET /logout failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusFound {
		t.Fatalf("expected status 302 on logout, got %d", resp.StatusCode)
	}

	if loc := resp.Header.Get("Location"); loc != "/oauth/login" {
		t.Fatalf("expected redirect to /oauth/login, got %q", loc)
	}

	// Check cleared cookie
	var clearedCookie *http.Cookie
	for _, c := range resp.Cookies() {
		if c.Name == rig.sessionManager.CookieName {
			clearedCookie = c
			break
		}
	}
	if clearedCookie == nil {
		t.Fatalf("expected cleared session cookie in response")
	}
	if clearedCookie.MaxAge > 0 || clearedCookie.Value != "" {
		t.Errorf("expected cookie to be cleared, got value %q and maxAge %d", clearedCookie.Value, clearedCookie.MaxAge)
	}
}

func TestStaticAssets(t *testing.T) {
	rig := setupTestRig(t)

	resp, err := rig.client.Get(rig.portalServer.URL + "/static/styles.css")
	if err != nil {
		t.Fatalf("GET /static/styles.css failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected status 200, got %d", resp.StatusCode)
	}

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("failed to read styles: %v", err)
	}
	if !strings.Contains(string(bodyBytes), "color-scheme") {
		t.Errorf("expected styles.css to contain 'color-scheme'")
	}
}
