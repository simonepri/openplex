// Tests atomic lineage claim acquisition, renewal, expiration, and agent authentication fencing.

package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type lineageAuthenticator map[string]identity

type lineageTestClock struct {
	monotonic time.Time
	wall      time.Time
}

func (a lineageAuthenticator) authenticate(_ context.Context, token string) (identity, error) {
	return a[token], nil
}

func TestLineageClaimFencesExpiredAndStaleBuilds(t *testing.T) {
	s, clock := lineageServer(t)
	nonceA := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	nonceBBytes := make([]byte, 32)
	nonceBBytes[0] = 1
	nonceB := base64.RawURLEncoding.EncodeToString(nonceBBytes)

	first := lineageRequestCall(t, s, "acquire", "agent-a", nonceA, 0)
	if first.Code != http.StatusOK || first.Body.String() != "1" {
		t.Fatalf("initial lineage acquire returned %d", first.Code)
	}
	retry := lineageRequestCall(t, s, "acquire", "agent-a", nonceA, 0)
	state, err := readLineageClaims(s.lineagePath)
	if err != nil {
		t.Fatal(err)
	}
	if retry.Code != http.StatusOK || retry.Body.String() != "1" || len(state.Claims) != 1 || state.Claims[0].Generation != 1 {
		t.Fatalf("lost-response retry was not idempotent: %d %#v", retry.Code, state.Claims)
	}
	if conflict := lineageRequestCall(t, s, "acquire", "agent-b", nonceB, 0); conflict.Code != http.StatusConflict {
		t.Fatalf("concurrent writer returned %d", conflict.Code)
	}

	clock.monotonic = clock.monotonic.Add(lineageClaimTTL + time.Nanosecond)
	clock.wall = clock.wall.Add(lineageClaimTTL + time.Nanosecond)
	if expiredRenew := lineageRequestCall(t, s, "renew", "agent-a", nonceA, 1); expiredRenew.Code != http.StatusConflict {
		t.Fatalf("expired writer renewed with %d", expiredRenew.Code)
	}
	second := lineageRequestCall(t, s, "acquire", "agent-b", nonceB, 0)
	state, err = readLineageClaims(s.lineagePath)
	if err != nil {
		t.Fatal(err)
	}
	if second.Code != http.StatusOK || second.Body.String() != "2" || len(state.Claims) != 1 || state.Claims[0].Generation != 2 {
		t.Fatalf("successor generation = %#v, status %d", state.Claims, second.Code)
	}
	if staleRelease := lineageRequestCall(t, s, "release", "agent-a", nonceA, 1); staleRelease.Code != http.StatusConflict {
		t.Fatalf("stale release returned %d", staleRelease.Code)
	}
	if renew := lineageRequestCall(t, s, "renew", "agent-b", nonceB, 2); renew.Code != http.StatusNoContent {
		t.Fatalf("successor could not renew after stale release: %d", renew.Code)
	}
}

func TestLineageClaimGenerationPreventsSameNonceABA(t *testing.T) {
	s, clock := lineageServer(t)
	nonce := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	if result := lineageRequestCall(t, s, "acquire", "agent-a", nonce, 0); result.Body.String() != "1" {
		t.Fatalf("initial generation = %q", result.Body.String())
	}
	clock.monotonic = clock.monotonic.Add(lineageClaimTTL + time.Nanosecond)
	clock.wall = clock.wall.Add(lineageClaimTTL + time.Nanosecond)
	if result := lineageRequestCall(t, s, "acquire", "agent-a", nonce, 0); result.Body.String() != "2" {
		t.Fatalf("successor generation = %q", result.Body.String())
	}
	if result := lineageRequestCall(t, s, "renew", "agent-a", nonce, 1); result.Code != http.StatusConflict {
		t.Fatalf("delayed renew returned %d", result.Code)
	}
	if result := lineageRequestCall(t, s, "release", "agent-a", nonce, 1); result.Code != http.StatusConflict {
		t.Fatalf("delayed release returned %d", result.Code)
	}
}

func TestLineageClaimIgnoresForwardWallClockJump(t *testing.T) {
	s, clock := lineageServer(t)
	nonceA := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	nonceBBytes := make([]byte, 32)
	nonceBBytes[0] = 1
	nonceB := base64.RawURLEncoding.EncodeToString(nonceBBytes)
	if result := lineageRequestCall(t, s, "acquire", "agent-a", nonceA, 0); result.Code != http.StatusOK {
		t.Fatalf("initial lineage acquire returned %d", result.Code)
	}
	clock.wall = clock.wall.Add(24 * time.Hour)
	if result := lineageRequestCall(t, s, "acquire", "agent-b", nonceB, 0); result.Code != http.StatusConflict {
		t.Fatalf("wall-clock jump admitted a concurrent writer with %d", result.Code)
	}
	if result := lineageRequestCall(t, s, "renew", "agent-a", nonceA, 1); result.Code != http.StatusNoContent {
		t.Fatalf("wall-clock jump expired the live holder with %d", result.Code)
	}
}

func TestLineageClaimRestartWaitsOneTTLBeforeRegrant(t *testing.T) {
	s, clock := lineageServer(t)
	nonceA := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	nonceBBytes := make([]byte, 32)
	nonceBBytes[0] = 1
	nonceB := base64.RawURLEncoding.EncodeToString(nonceBBytes)
	if result := lineageRequestCall(t, s, "acquire", "agent-a", nonceA, 0); result.Code != http.StatusOK {
		t.Fatalf("initial lineage acquire returned %d", result.Code)
	}

	restarted := s
	restarted.lineagesMu = &sync.Mutex{}
	restarted.lineageDeadlines = map[string]time.Time{}
	restarted.lineageStarted = clock.monotonic
	clock.wall = clock.wall.Add(24 * time.Hour)
	if result := lineageRequestCall(t, restarted, "acquire", "agent-b", nonceB, 0); result.Code != http.StatusConflict {
		t.Fatalf("restart fence admitted a writer with %d", result.Code)
	}
	clock.monotonic = clock.monotonic.Add(lineageClaimTTL + time.Nanosecond)
	if result := lineageRequestCall(t, restarted, "acquire", "agent-b", nonceB, 0); result.Code != http.StatusOK || result.Body.String() != "2" {
		t.Fatalf("writer was not admitted after restart fence: %d %q", result.Code, result.Body.String())
	}
}

func TestLineageClaimSurvivesInterruptedTemporaryWrite(t *testing.T) {
	s, _ := lineageServer(t)
	nonce := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	if result := lineageRequestCall(t, s, "acquire", "agent-a", nonce, 0); result.Code != http.StatusOK {
		t.Fatalf("initial lineage acquire returned %d", result.Code)
	}
	if err := os.WriteFile(s.lineagePath+".tmp", []byte("truncated"), 0600); err != nil {
		t.Fatal(err)
	}
	restarted := s
	restarted.lineagesMu = &sync.Mutex{}
	restarted.lineageDeadlines = map[string]time.Time{}
	restarted.lineageStarted = restarted.lineageMonotonicNow()
	if result := lineageRequestCall(t, restarted, "renew", "agent-a", nonce, 1); result.Code != http.StatusNoContent {
		t.Fatalf("durable lineage claim did not survive restart: %d", result.Code)
	}
}

func lineageServer(t *testing.T) (server, *lineageTestClock) {
	t.Helper()
	directory := t.TempDir()
	clock := &lineageTestClock{
		monotonic: time.Date(2026, 9, 3, 10, 15, 30, 0, time.UTC),
		wall:      time.Date(2026, 9, 3, 10, 15, 30, 0, time.UTC),
	}
	owner := fixtureUserID
	workspace := "123e4567-e89b-42d3-a456-426614174000"
	base := identity{
		Cell: "cell-eaws-lh1", Incarnation: "eaws-lh1",
		Lineage: "0123456789abcdef0123456789abcdef01234567", Machine: "ldap-dev",
		SubjectID: owner, Team: "examples", Workspace: "dev", WorkspaceID: workspace,
		WorkspaceVolume: "/var/lib/workspace",
	}
	agentA := base
	agentA.AgentID = "223e4567-e89b-42d3-a456-426614174000"
	agentB := base
	agentB.AgentID = "323e4567-e89b-42d3-a456-426614174000"
	bindingPath := filepath.Join(directory, "bindings.json")
	if err := writeBindings(bindingPath, []binding{{
		Issuer: "https://issuer.example", Subject: "dex-subject",
		PreferredUsername: "ldap", OwnerID: owner,
	}}); err != nil {
		t.Fatal(err)
	}
	return server{
		authenticator:       lineageAuthenticator{"agent-a": agentA, "agent-b": agentB},
		bindingPath:         bindingPath,
		lineagePath:         filepath.Join(directory, "lineages.json"),
		lineageWallNow:      func() time.Time { return clock.wall },
		lineageMonotonicNow: func() time.Time { return clock.monotonic },
		lineageStarted:      clock.monotonic,
		lineageDeadlines:    map[string]time.Time{},
		bindingsMu:          &sync.Mutex{}, lineagesMu: &sync.Mutex{},
	}, clock
}

func lineageRequestCall(t *testing.T, s server, operation, token, nonce string, generation uint64) *httptest.ResponseRecorder {
	t.Helper()
	body := `{"nonce":"` + nonce + `"}`
	if generation != 0 {
		body = fmt.Sprintf(`{"nonce":%q,"generation":%d}`, nonce, generation)
	}
	req := httptest.NewRequest(
		http.MethodPost,
		"/v1/lineages/"+operation,
		strings.NewReader(body),
	)
	req.Header.Set("Authorization", "Bearer "+token)
	response := httptest.NewRecorder()
	s.mutateLineage(response, req, operation)
	return response
}

func TestLineageClaimAcceptsCanonicalLineage(t *testing.T) {
	s, _ := lineageServer(t)
	agent := s.authenticator.(lineageAuthenticator)["agent-a"]
	agent.Lineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	s.authenticator.(lineageAuthenticator)["agent-a"] = agent

	nonce := base64.RawURLEncoding.EncodeToString(make([]byte, 32))
	res := lineageRequestCall(t, s, "acquire", "agent-a", nonce, 0)
	if res.Code != http.StatusOK || res.Body.String() != "1" {
		t.Fatalf("canonical lineage acquire returned %d: %s", res.Code, res.Body.String())
	}
}

