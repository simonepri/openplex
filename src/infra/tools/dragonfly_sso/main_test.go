// Tests the constrained Dragonfly SSO bridge identity, role, route, and session boundaries.

package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type roundTripperFunc func(req *http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func mockClient(fn func(req *http.Request) (*http.Response, error)) *http.Client {
	return &http.Client{
		Transport: roundTripperFunc(fn),
	}
}

func jsonResponse(statusCode int, body any) *http.Response {
	var b []byte
	if body != nil {
		b, _ = json.Marshal(body)
	}
	return &http.Response{
		StatusCode: statusCode,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(bytes.NewReader(b)),
	}
}

func decodeSegment(segment string) (map[string]any, error) {
	decoded, err := base64.RawURLEncoding.DecodeString(segment)
	if err != nil {
		return nil, err
	}
	var res map[string]any
	if err := json.Unmarshal(decoded, &res); err != nil {
		return nil, err
	}
	return res, nil
}

// 1. AdministrativeSessionTest
func TestInternalAdministrationUsesTheSeededRootIdentity(t *testing.T) {
	token := administrativeToken("cell-key", "ctrl-eaws-lh1")
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		t.Fatalf("expected 3 parts in JWT, got %d", len(parts))
	}
	claims, err := decodeSegment(parts[1])
	if err != nil {
		t.Fatalf("failed to decode claims: %v", err)
	}
	idVal, ok := claims["id"].(float64)
	if !ok || int64(idVal) != rootUserID {
		t.Errorf("expected id %d, got %v", rootUserID, claims["id"])
	}
	cellVal, ok := claims["cell"].(string)
	if !ok || cellVal != "ctrl-eaws-lh1" {
		t.Errorf("expected cell 'ctrl-eaws-lh1', got %v", claims["cell"])
	}
}

// 2. IdentityMappingTest
func TestIdentityIncludesIssuerAndFullCollisionResistantDigest(t *testing.T) {
	identity := Identity{
		Issuer:            "https://issuer.example",
		Subject:           "subject",
		PreferredUsername: "user",
		Email:             "user@example.com",
	}
	expectedHash := sha256.Sum256([]byte(`["https://issuer.example","subject"]`))
	expectedDigest := hex.EncodeToString(expectedHash[:])
	expectedEmail := expectedDigest + "@oidc.invalid"

	if got := mappedEmail(identity); got != expectedEmail {
		t.Errorf("mappedEmail = %q, want %q", got, expectedEmail)
	}
	parts := strings.Split(mappedEmail(identity), "@")
	if len(parts[0]) != 64 {
		t.Errorf("expected 64 hex characters, got %d", len(parts[0]))
	}

	migrated := Identity{
		Issuer:            "https://new-issuer.example",
		Subject:           "subject",
		PreferredUsername: "user",
		Email:             "user@example.com",
	}
	if mappedEmail(identity) == mappedEmail(migrated) {
		t.Errorf("mappedEmail collision between distinct issuers")
	}
}

func TestOperatorGroupIsRequired(t *testing.T) {
	headers := make(http.Header)
	headers.Set("X-Forwarded-User", "immutable-subject")
	headers.Set("X-Forwarded-Preferred-Username", "operator")
	headers.Set("X-Forwarded-Email", "operator@unit.test")
	headers.Set("X-Forwarded-Groups", base64.StdEncoding.EncodeToString([]byte(`["team:examples"]`)))

	_, err := assertedIdentity(headers, "https://dex.example", "operators", "", true)
	if err == nil {
		t.Fatalf("expected error when operator group is missing, got nil")
	}

	headers.Set("X-Forwarded-Groups", base64.StdEncoding.EncodeToString([]byte(`["team:examples","operators"]`)))
	id, err := assertedIdentity(headers, "https://dex.example", "operators", "", true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := Identity{
		Issuer:            "https://dex.example",
		Subject:           "immutable-subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}
	if id != expected {
		t.Errorf("assertedIdentity = %+v, want %+v", id, expected)
	}

	headers.Del("X-Forwarded-Preferred-Username")
	id, err = assertedIdentity(headers, "https://dex.example", "operators", "", true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if id != expected {
		t.Errorf("assertedIdentity without preferred username = %+v, want %+v", id, expected)
	}

	headers.Set("X-Forwarded-Groups", base64.StdEncoding.EncodeToString([]byte(`["operators@example.com"]`)))
	id, err = assertedIdentity(headers, "https://dex.example", "operators", "", true)
	if err != nil {
		t.Fatalf("unexpected error with group prefix: %v", err)
	}
	if id != expected {
		t.Errorf("assertedIdentity with group prefix = %+v, want %+v", id, expected)
	}

	headers.Del("X-Forwarded-Groups")
	id, err = assertedIdentity(headers, "https://dex.example", "operators", "operator@unit.test", true)
	if err != nil {
		t.Fatalf("unexpected error with operator emails: %v", err)
	}
	if id != expected {
		t.Errorf("assertedIdentity with operator emails = %+v, want %+v", id, expected)
	}

	id, err = assertedIdentity(headers, "https://dex.example", "operators", "OTHER@EXAMPLE.COM, OPERATOR@UNIT.TEST", true)
	if err != nil {
		t.Fatalf("unexpected error with case-insensitive operator emails: %v", err)
	}
	if id != expected {
		t.Errorf("assertedIdentity with multiple operator emails = %+v, want %+v", id, expected)
	}
}

func TestEnvoyHeadersPreserveIdentityAndRejectMalformedOrNonoperatorGroups(t *testing.T) {
	headers := make(http.Header)
	headers.Set("x-forwarded-user", "immutable-subject")
	headers.Set("x-forwarded-preferred-username", "operator")
	headers.Set("x-forwarded-email", "operator@unit.test")
	headers.Set("x-forwarded-groups", base64.StdEncoding.EncodeToString([]byte(`["team:examples","operators"]`)))

	id, err := assertedIdentity(headers, "https://dex.example", "operators", "", true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := Identity{
		Issuer:            "https://dex.example",
		Subject:           "immutable-subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}
	if id != expected {
		t.Errorf("assertedIdentity = %+v, want %+v", id, expected)
	}

	invalid := []string{
		"",
		"operators",
		`["operators"]`,
		"%%%",
		base64.StdEncoding.EncodeToString([]byte("not-json")),
		base64.StdEncoding.EncodeToString([]byte(`{"operators":true}`)),
		base64.StdEncoding.EncodeToString([]byte(`["operators",1]`)),
		base64.StdEncoding.EncodeToString([]byte(`["team:examples"]`)),
	}

	for _, g := range invalid {
		t.Run(fmt.Sprintf("invalid_group_%s", g), func(t *testing.T) {
			h := headers.Clone()
			h.Set("x-forwarded-groups", g)
			_, err := assertedIdentity(h, "https://dex.example", "operators", "", true)
			if err == nil {
				t.Errorf("expected error for groups %q, got nil", g)
			}
		})
	}
}

func TestIssuerMigrationWithSameProfileFailsClosed(t *testing.T) {
	server := NewServer(Config{})
	identity := Identity{
		Issuer:            "https://new.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}
	oldIdentity := Identity{
		Issuer:            "https://old.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             identity.Email,
	}

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		return jsonResponse(http.StatusOK, []map[string]any{
			{
				"bio":   profileMarker(identity.Email),
				"email": mappedEmail(oldIdentity),
			},
		}), nil
	})

	_, err := server.findUser(context.Background(), identity, "root-token")
	if err == nil || !strings.Contains(err.Error(), "another OIDC identity") {
		t.Fatalf("expected error containing 'another OIDC identity', got %v", err)
	}
}

func TestExistingNativeEmailForAnotherSubjectFailsClosed(t *testing.T) {
	server := NewServer(Config{})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		return jsonResponse(http.StatusOK, []map[string]any{
			{"bio": "", "email": identity.Email, "id": 4},
		}), nil
	})

	_, err := server.findUser(context.Background(), identity, "root-token")
	if err == nil || !strings.Contains(err.Error(), "another OIDC identity") {
		t.Fatalf("expected error containing 'another OIDC identity', got %v", err)
	}
}

func TestExistingUsernameForAnotherSubjectFailsClosed(t *testing.T) {
	server := NewServer(Config{})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "new@example.com",
	}

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		return jsonResponse(http.StatusOK, []map[string]any{
			{"bio": "", "email": "other@example.com", "name": "operator"},
		}), nil
	})

	_, err := server.findUser(context.Background(), identity, "root-token")
	if err == nil || !strings.Contains(err.Error(), "another OIDC identity") {
		t.Fatalf("expected error containing 'another OIDC identity', got %v", err)
	}
}

func TestImmutableSubjectMarkerIgnoresChangedDisplayClaims(t *testing.T) {
	server := NewServer(Config{})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "new-name",
		Email:             "new@example.com",
	}
	user := ManagerUser{
		Bio:   subjectMarker(identity),
		Email: "old@example.com",
		ID:    7,
		Name:  "old-name",
	}

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		return jsonResponse(http.StatusOK, []ManagerUser{user}), nil
	})

	found, err := server.findUser(context.Background(), identity, "root-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if found == nil || *found != user {
		t.Fatalf("found = %+v, want %+v", found, user)
	}
}

func TestExactRoleIsCreatedWithoutGuestPermissions(t *testing.T) {
	server := NewServer(Config{})
	var calls []string
	var postBody map[string]any

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		calls = append(calls, fmt.Sprintf("%s %s", req.Method, req.URL.Path))
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly" && len(calls) == 1:
			return jsonResponse(http.StatusOK, [][]string{}), nil
		case req.Method == http.MethodPost && req.URL.Path == "/api/v1/roles":
			b, _ := io.ReadAll(req.Body)
			_ = json.Unmarshal(b, &postBody)
			return jsonResponse(http.StatusOK, map[string]any{}), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		default:
			return jsonResponse(http.StatusNotFound, nil), nil
		}
	})

	err := server.ensureRole(context.Background(), "root-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if postBody["role"] != ssoRole {
		t.Errorf("role in post body = %v, want %s", postBody["role"], ssoRole)
	}
	permsRaw, ok := postBody["permissions"].([]any)
	if !ok {
		t.Fatalf("permissions is not a list: %v", postBody["permissions"])
	}
	actualPerms := make(map[permissionPair]bool)
	for _, p := range permsRaw {
		pMap := p.(map[string]any)
		actualPerms[permissionPair{
			resource: pMap["object"].(string),
			action:   pMap["action"].(string),
		}] = true
	}
	if !rolesMatch(actualPerms) {
		t.Errorf("permissions created do not match expected: %+v", actualPerms)
	}
	if actualPerms[permissionPair{resource: "personal-access-tokens", action: "read"}] {
		t.Errorf("personal-access-tokens:read should not be in role permissions")
	}
}

func TestMutableEmailUpdatesProfileWithoutChangingIdentity(t *testing.T) {
	server := NewServer(Config{
		JWTKey: "jwt-key",
		CellID: "ctrl-eaws-lh1",
	})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "new@example.com",
	}
	immutableEmail := mappedEmail(identity)
	user := ManagerUser{
		Bio:   profileMarker("old@example.com"),
		Email: immutableEmail,
		ID:    7,
		Name:  "opaque-old-name",
		State: "enable",
	}

	var patchBody map[string]any
	var patchMethod, patchPath string

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			return jsonResponse(http.StatusOK, []ManagerUser{user}), nil
		case req.Method == http.MethodPatch && strings.HasPrefix(req.URL.Path, "/api/v1/users/"):
			patchMethod = req.Method
			patchPath = req.URL.Path
			b, _ := io.ReadAll(req.Body)
			_ = json.Unmarshal(b, &patchBody)
			return jsonResponse(http.StatusOK, map[string]any{}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			return jsonResponse(http.StatusNotFound, nil), nil
		}
	})

	userID, err := server.ensureUser(context.Background(), identity)
	if err != nil {
		t.Fatalf("ensureUser failed: %v", err)
	}
	if userID != 7 {
		t.Errorf("userID = %d, want 7", userID)
	}
	if patchMethod != "PATCH" || patchPath != "/api/v1/users/7" {
		t.Errorf("expected PATCH /api/v1/users/7, got %s %s", patchMethod, patchPath)
	}
	if patchBody["bio"] != subjectMarker(identity) || patchBody["email"] != "new@example.com" || patchBody["name"] != "operator" {
		t.Errorf("unexpected patch body: %+v", patchBody)
	}
}

func TestRoleEscalationIsRemovedAndExactRoleRechecked(t *testing.T) {
	server := NewServer(Config{
		JWTKey: "jwt-key",
		CellID: "ctrl-eaws-lh1",
	})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}
	user := ManagerUser{
		Bio:   subjectMarker(identity),
		Email: identity.Email,
		ID:    7,
		Name:  identity.PreferredUsername,
		State: "enable",
	}

	var calls []string
	rolesGetCount := 0

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		calls = append(calls, fmt.Sprintf("%s %s", req.Method, req.URL.Path))
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			return jsonResponse(http.StatusOK, []ManagerUser{user}), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users/7/roles":
			rolesGetCount++
			if rolesGetCount == 1 {
				return jsonResponse(http.StatusOK, []string{"guest", "root"}), nil
			}
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		case req.Method == http.MethodDelete && strings.HasPrefix(req.URL.Path, "/api/v1/users/7/roles/"):
			return jsonResponse(http.StatusNoContent, nil), nil
		case req.Method == http.MethodPut && req.URL.Path == "/api/v1/users/7/roles/console-readonly":
			return jsonResponse(http.StatusNoContent, nil), nil
		default:
			return jsonResponse(http.StatusNotFound, nil), nil
		}
	})

	userID, err := server.ensureUser(context.Background(), identity)
	if err != nil {
		t.Fatalf("ensureUser failed: %v", err)
	}
	if userID != 7 {
		t.Errorf("userID = %d, want 7", userID)
	}

	hasDeleteRoot := false
	hasPutSSORole := false
	for _, call := range calls {
		if call == "DELETE /api/v1/users/7/roles/root" {
			hasDeleteRoot = true
		}
		if call == "PUT /api/v1/users/7/roles/console-readonly" {
			hasPutSSORole = true
		}
	}
	if !hasDeleteRoot {
		t.Errorf("missing DELETE /api/v1/users/7/roles/root call: %v", calls)
	}
	if !hasPutSSORole {
		t.Errorf("missing PUT /api/v1/users/7/roles/console-readonly call: %v", calls)
	}
}

func TestConcurrentRequestsDoNotInvalidateRootSessions(t *testing.T) {
	server := NewServer(Config{
		JWTKey: "jwt-key",
		CellID: "ctrl-eaws-lh1",
	})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}

	var ensureCount atomic.Int64
	user := ManagerUser{
		Bio:   subjectMarker(identity),
		Email: identity.Email,
		ID:    7,
		Name:  identity.PreferredUsername,
		State: "enable",
	}

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		time.Sleep(10 * time.Millisecond)
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			ensureCount.Add(1)
			return jsonResponse(http.StatusOK, []ManagerUser{user}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			return jsonResponse(http.StatusNotFound, nil), nil
		}
	})

	var wg sync.WaitGroup
	results := make([]int64, 4)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			id, err := server.resolveUser(context.Background(), identity)
			if err != nil {
				t.Errorf("resolveUser error: %v", err)
			}
			results[idx] = id
		}(i)
	}
	wg.Wait()

	for _, id := range results {
		if id != 7 {
			t.Errorf("got userID %d, want 7", id)
		}
	}
	if ensureCount.Load() != 1 {
		t.Errorf("ensureUser ran %d times, want 1", ensureCount.Load())
	}
}

func TestCachedIdentityIsRevalidatedAfterSessionExpiry(t *testing.T) {
	server := NewServer(Config{
		JWTKey: "jwt-key",
		CellID: "ctrl-eaws-lh1",
	})
	identity := Identity{
		Issuer:            "https://dex.example",
		Subject:           "subject",
		PreferredUsername: "operator",
		Email:             "operator@unit.test",
	}

	var currentTime time.Time
	server.nowFunc = func() time.Time {
		return currentTime
	}

	callCount := 0
	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			callCount++
			user := ManagerUser{
				Bio:   subjectMarker(identity),
				Email: identity.Email,
				ID:    int64(6 + callCount),
				Name:  identity.PreferredUsername,
				State: "enable",
			}
			return jsonResponse(http.StatusOK, []ManagerUser{user}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			return jsonResponse(http.StatusNotFound, nil), nil
		}
	})

	currentTime = time.Unix(100, 0)
	id1, err := server.resolveUser(context.Background(), identity)
	if err != nil || id1 != 7 {
		t.Fatalf("first call returned %d, err: %v", id1, err)
	}

	currentTime = time.Unix(101, 0)
	id2, err := server.resolveUser(context.Background(), identity)
	if err != nil || id2 != 7 {
		t.Fatalf("second call (cached) returned %d, err: %v", id2, err)
	}

	currentTime = time.Unix(100+sessionSeconds, 0)
	id3, err := server.resolveUser(context.Background(), identity)
	if err != nil || id3 != 8 {
		t.Fatalf("third call (expired) returned %d, err: %v", id3, err)
	}

	if callCount != 2 {
		t.Errorf("expected 2 backend calls, got %d", callCount)
	}
}

func TestCachedUsersRemainSeparatedByOIDCIdentity(t *testing.T) {
	server := NewServer(Config{
		JWTKey: "jwt-key",
		CellID: "ctrl-eaws-lh1",
	})
	first := Identity{Issuer: "https://dex.example", Subject: "first", PreferredUsername: "first", Email: "first@unit.test"}
	second := Identity{Issuer: "https://dex.example", Subject: "second", PreferredUsername: "second", Email: "second@unit.test"}

	callCount := 0
	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			callCount++
			id := int64(7)
			email := first.Email
			name := first.PreferredUsername
			bio := subjectMarker(first)
			if strings.Contains(req.Header.Get("Authorization"), "Bearer") && callCount == 2 {
				id = 9
				email = second.Email
				name = second.PreferredUsername
				bio = subjectMarker(second)
			}
			return jsonResponse(http.StatusOK, []ManagerUser{
				{Bio: bio, Email: email, ID: id, Name: name, State: "enable"},
			}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			return jsonResponse(http.StatusNotFound, nil), nil
		}
	})

	id1, _ := server.resolveUser(context.Background(), first)
	if id1 != 7 {
		t.Errorf("first user id = %d, want 7", id1)
	}
	id2, _ := server.resolveUser(context.Background(), second)
	if id2 != 9 {
		t.Errorf("second user id = %d, want 9", id2)
	}
	id1Cached, _ := server.resolveUser(context.Background(), first)
	if id1Cached != 7 {
		t.Errorf("first user cached id = %d, want 7", id1Cached)
	}
	if callCount != 2 {
		t.Errorf("expected 2 backend calls, got %d", callCount)
	}
}

// 3. RequestPolicyTest
func TestReadOnlyConsoleAllowlistIsExact(t *testing.T) {
	allowed := []string{
		"/",
		"/static/js/main.01234567.js",
		"/fonts/MabryPro-Light.ttf",
		"/favicon/favicon.ico",
		"/asset-manifest.json",
		"/clusters/1/schedulers/2",
		"/resource/persistent-cache-task/clusters/1/task-2",
		"/api/v1/clusters",
		"/api/v1/clusters/1",
		"/api/v1/scheduler-clusters",
		"/api/v1/scheduler-features",
		"/api/v1/schedulers?cluster_id=1",
		"/api/v1/seed-peer-clusters",
		"/api/v1/seed-peers",
		"/api/v1/peers",
		"/api/v1/jobs/abc-123",
		"/api/v1/persistent-cache-tasks",
		"/api/v1/audits",
		"/api/v1/users?page=1&per_page=10000000",
		"/api/v1/users/7",
		"/api/v1/users/7/roles",
	}

	for _, target := range allowed {
		t.Run("allowed_"+target, func(t *testing.T) {
			if !allowedRequest("GET", target, 7, nil) {
				t.Errorf("target %q should be allowed", target)
			}
		})
	}
}

func TestPatOapiNativeAuthAndAdministrationAreDenied(t *testing.T) {
	denied := []struct {
		method string
		target string
	}{
		{"GET", "/api/v1/personal-access-tokens"},
		{"GET", "/api/v1/personal-access-tokens/1"},
		{"GET", "/oapi/v1/jobs"},
		{"POST", "/api/v1/users/signin"},
		{"POST", "/api/v1/users/signup"},
		{"POST", "/api/v1/users/7/reset_password"},
		{"GET", "/api/v1/users/8"},
		{"GET", "/api/v1/roles"},
		{"GET", "/api/v1/permissions"},
		{"GET", "/api%2Fv1%2Fpersonal-access-tokens"},
		{"GET", "/metrics"},
		{"GET", "/assets/../api/v1/users"},
		{"GET", "/./api/v1/users"},
		{"GET", "//api/v1/users"},
		{"GET", "https://dragonfly.example/api/v1/clusters"},
		{"GET", "/%2e%2e/api/v1/clusters"},
		{"GET", "/static%2fjs/main.js"},
		{"GET", "/clusters/new"},
		{"GET", "/clusters/1/edit"},
		{"GET", "/developer/personal-access-tokens"},
		{"GET", "/developer/personal-access-tokens/new"},
		{"GET", "/jobs/preheats/new"},
		{"GET", "/signin"},
		{"GET", "/signup"},
		{"GET", "/users/new"},
		{"PATCH", "/api/v1/clusters/1"},
	}

	for _, tc := range denied {
		t.Run("denied_"+tc.method+"_"+tc.target, func(t *testing.T) {
			if allowedRequest(tc.method, tc.target, 7, nil) {
				t.Errorf("%s %q should be denied", tc.method, tc.target)
			}
		})
	}
}

func TestAuditPageRequestsPassOidcBridge(t *testing.T) {
	server := NewServer(Config{
		CellID:        "ctrl-eaws-lh1",
		JWTKey:        "jwt-key",
		OIDCIssuer:    "https://dex.unit.test",
		OperatorGroup: "operators",
	})

	proxyCount := 0
	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.Header.Get("Content-Type") == "application/json" && req.URL.Path == "/api/v1/users":
			return jsonResponse(http.StatusOK, []ManagerUser{
				{Bio: subjectMarker(Identity{Issuer: "https://dex.unit.test", Subject: "dex-subject"}), Email: "operator@unit.test", ID: 7, Name: "operator", State: "enable"},
			}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			proxyCount++
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`{"status":"ok"}`)),
			}, nil
		}
	})

	targets := []string{
		"/api/v1/users?page=1&per_page=10000000",
		"/api/v1/audits?page=1&per_page=10",
	}

	for _, target := range targets {
		t.Run("audit_pass_"+target, func(t *testing.T) {
			req := httptest.NewRequest("GET", target, nil)
			req.Header.Set("x-forwarded-email", "operator@unit.test")
			req.Header.Set("x-forwarded-groups", base64.StdEncoding.EncodeToString([]byte(`["operators"]`)))
			req.Header.Set("x-forwarded-preferred-username", "operator")
			req.Header.Set("x-forwarded-user", "dex-subject")
			req.RemoteAddr = "127.0.0.1:12345"

			rec := httptest.NewRecorder()
			server.ServeHTTP(rec, req)

			if rec.Code != http.StatusOK {
				t.Errorf("got status %d, want 200, body: %s", rec.Code, rec.Body.String())
			}
		})
	}
	if proxyCount != 2 {
		t.Errorf("proxy called %d times, want 2", proxyCount)
	}
}

func TestQueryTokenReplayIsDeniedCaseInsensitively(t *testing.T) {
	for _, key := range []string{"access_token", "ACCESS_TOKEN", "token"} {
		target := fmt.Sprintf("/api/v1/clusters?%s=stolen", key)
		if allowedRequest("GET", target, 7, nil) {
			t.Errorf("query token replay for key %q should be denied", key)
		}
	}
	if allowedRequest("GET", "/api/v1/clusters?x=1;access_token=x", 7, nil) {
		t.Errorf("semicolon in query parameter should be denied")
	}
}

func TestAlternateHeaderAndCookieCredentialsAreDenied(t *testing.T) {
	h1 := make(http.Header)
	h1.Set("Authorization", "Bearer stolen")
	if allowedRequest("GET", "/api/v1/clusters", 7, h1) {
		t.Errorf("Authorization header should be denied")
	}

	h2 := make(http.Header)
	h2.Set("Cookie", "access_token=stolen")
	if allowedRequest("GET", "/api/v1/clusters", 7, h2) {
		t.Errorf("access_token cookie should be denied")
	}

	h3 := make(http.Header)
	h3.Set("Cookie", "jwt=console-session; _dragonfly_console=proxy-session")
	if !allowedRequest("GET", "/api/v1/clusters", 7, h3) {
		t.Errorf("legitimate cookies should be allowed")
	}
}

func TestBrowserCookieAndAuthorizationAreNeverForwarded(t *testing.T) {
	if forwardedHeaders["cookie"] {
		t.Errorf("cookie must not be forwarded")
	}
	if forwardedHeaders["authorization"] {
		t.Errorf("authorization must not be forwarded")
	}
}

func TestSPARoutesUseTheManagerEntryPoint(t *testing.T) {
	for _, target := range []string{"/", "/clusters", "/clusters/1/schedulers/2"} {
		if got := managerTarget(target); got != "/" {
			t.Errorf("managerTarget(%q) = %q, want '/'", target, got)
		}
	}
	if got := managerTarget("/api/v1/clusters?page=1"); got != "/api/v1/clusters?page=1" {
		t.Errorf("managerTarget API route = %q, want unchanged", got)
	}
	if got := managerTarget("/static/js/main.js"); got != "/static/js/main.js" {
		t.Errorf("managerTarget static route = %q, want unchanged", got)
	}
}

// 4. SessionTest
func TestShortTokenExpiryAndCrossUserBinding(t *testing.T) {
	first := userToken(41, "cell-a-key", "cell-a", 1000)
	second := userToken(42, "cell-a-key", "cell-a", 1000)

	claims, err := decodeSegment(strings.Split(first, ".")[1])
	if err != nil {
		t.Fatalf("failed to decode claims: %v", err)
	}
	if claims["cell"] != "cell-a" || claims["exp"].(float64) != 1900 || claims["id"].(float64) != 41 || claims["orig_iat"].(float64) != 1000 {
		t.Errorf("unexpected claims in first token: %+v", claims)
	}
	if first == second {
		t.Errorf("expected different tokens for different user IDs")
	}
}

func TestCellKeysCryptographicallySeparateTokens(t *testing.T) {
	token := userToken(41, "cell-a-key", "cell-a", 1000)
	parts := strings.Split(token, ".")
	unsigned := parts[0] + "." + parts[1]

	mac := hmac.New(sha256.New, []byte("cell-b-key"))
	mac.Write([]byte(unsigned))
	wrongSig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))

	if parts[2] == wrongSig {
		t.Errorf("signatures matched across different cell keys")
	}
}

func TestSignoutExpiresManagerAndOAuthCookiesWithoutMinting(t *testing.T) {
	server := NewServer(Config{
		OAuthCookieName: "_dragonfly_console",
	})
	req := httptest.NewRequest("POST", "/api/v1/users/signout", nil)
	rec := httptest.NewRecorder()

	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("signout status = %d, want 200", rec.Code)
	}
	cookies := rec.Header().Values("Set-Cookie")
	if len(cookies) != 2 {
		t.Fatalf("expected 2 Set-Cookie headers, got %d: %v", len(cookies), cookies)
	}
	for _, c := range cookies {
		if !strings.Contains(c, "Max-Age=0") {
			t.Errorf("cookie %q does not contain Max-Age=0", c)
		}
		if strings.Contains(c, "jwt=") && c != "jwt=; Max-Age=0; Path=/; Secure; SameSite=Lax" {
			t.Errorf("unexpected jwt cookie: %q", c)
		}
	}
}

// 5. ForwardedHeadersTrustTest
func TestUntrustedClientIPWithForwardedHeadersIsRejected(t *testing.T) {
	server := NewServer(Config{
		CellID:        "ctrl-eaws-lh1",
		JWTKey:        "jwt-key",
		OIDCIssuer:    "https://dex.unit.test",
		OperatorGroup: "operators",
	})
	req := httptest.NewRequest("GET", "/api/v1/audits", nil)
	req.Header.Set("x-forwarded-email", "operator@unit.test")
	req.Header.Set("x-forwarded-groups", base64.StdEncoding.EncodeToString([]byte(`["operators"]`)))
	req.Header.Set("x-forwarded-preferred-username", "operator")
	req.Header.Set("x-forwarded-user", "dex-subject")
	req.RemoteAddr = "198.51.100.23:45678"

	rec := httptest.NewRecorder()
	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("got status %d, want 401 Unauthorized", rec.Code)
	}
}

func TestTrustedProxyIPAllowsForwardedHeaders(t *testing.T) {
	server := NewServer(Config{
		CellID:         "ctrl-eaws-lh1",
		JWTKey:         "jwt-key",
		OIDCIssuer:     "https://dex.unit.test",
		OperatorGroup:  "operators",
		TrustedProxies: "10.244.0.0/16",
	})

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			return jsonResponse(http.StatusOK, []ManagerUser{
				{Bio: subjectMarker(Identity{Issuer: "https://dex.unit.test", Subject: "dex-subject"}), Email: "operator@unit.test", ID: 7, Name: "operator", State: "enable"},
			}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`[]`)),
			}, nil
		}
	})

	req := httptest.NewRequest("GET", "/api/v1/audits", nil)
	req.Header.Set("x-forwarded-email", "operator@unit.test")
	req.Header.Set("x-forwarded-groups", base64.StdEncoding.EncodeToString([]byte(`["operators"]`)))
	req.Header.Set("x-forwarded-preferred-username", "operator")
	req.Header.Set("x-forwarded-user", "dex-subject")
	req.RemoteAddr = "10.244.0.15:45678"

	rec := httptest.NewRecorder()
	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("got status %d, want 200, body: %s", rec.Code, rec.Body.String())
	}
}

func TestCryptographicallyVerifiedForwardedHeadersAllowedFromAnyIP(t *testing.T) {
	server := NewServer(Config{
		CellID:        "ctrl-eaws-lh1",
		JWTKey:        "jwt-key",
		OIDCIssuer:    "https://dex.unit.test",
		OperatorGroup: "operators",
	})

	server.HTTPClient = mockClient(func(req *http.Request) (*http.Response, error) {
		switch {
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/roles/console-readonly":
			expected := make([][]string, len(expectedPermissions))
			for i, ep := range expectedPermissions {
				expected[i] = []string{ssoRole, ep.resource, ep.action}
			}
			return jsonResponse(http.StatusOK, expected), nil
		case req.Method == http.MethodGet && req.URL.Path == "/api/v1/users":
			return jsonResponse(http.StatusOK, []ManagerUser{
				{Bio: subjectMarker(Identity{Issuer: "https://dex.unit.test", Subject: "dex-subject"}), Email: "operator@unit.test", ID: 7, Name: "operator", State: "enable"},
			}), nil
		case req.Method == http.MethodGet && strings.HasSuffix(req.URL.Path, "/roles"):
			return jsonResponse(http.StatusOK, []string{ssoRole}), nil
		default:
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`[]`)),
			}, nil
		}
	})

	req := httptest.NewRequest("GET", "/api/v1/audits", nil)
	email := "operator@unit.test"
	groups := base64.StdEncoding.EncodeToString([]byte(`["operators"]`))
	username := "operator"
	user := "dex-subject"

	req.Header.Set("x-forwarded-email", email)
	req.Header.Set("x-forwarded-groups", groups)
	req.Header.Set("x-forwarded-preferred-username", username)
	req.Header.Set("x-forwarded-user", user)

	// Sorted: x-forwarded-email, x-forwarded-groups, x-forwarded-preferred-username, x-forwarded-user
	data := strings.Join([]string{email, groups, username, user}, ":")
	mac := hmac.New(sha256.New, []byte("jwt-key"))
	mac.Write([]byte(data))
	req.Header.Set("x-forwarded-signature", hex.EncodeToString(mac.Sum(nil)))

	req.RemoteAddr = "198.51.100.23:45678"

	rec := httptest.NewRecorder()
	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("got status %d, want 200, body: %s", rec.Code, rec.Body.String())
	}
}

func TestStaticForwardedVerificationHeaderRejected(t *testing.T) {
	server := NewServer(Config{
		CellID:        "ctrl-eaws-lh1",
		JWTKey:        "jwt-key",
		OIDCIssuer:    "https://dex.unit.test",
		OperatorGroup: "operators",
	})
	req := httptest.NewRequest("GET", "/api/v1/audits", nil)
	req.Header.Set("x-forwarded-email", "operator@unit.test")
	req.Header.Set("x-forwarded-groups", base64.StdEncoding.EncodeToString([]byte(`["operators"]`)))
	req.Header.Set("x-forwarded-preferred-username", "operator")
	req.Header.Set("x-forwarded-user", "dex-subject")

	mac := hmac.New(sha256.New, []byte("jwt-key"))
	mac.Write([]byte("dragonfly-sso-proxy"))
	req.Header.Set("x-forwarded-verification", hex.EncodeToString(mac.Sum(nil)))

	req.RemoteAddr = "198.51.100.23:45678"

	rec := httptest.NewRecorder()
	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("got status %d, want 401 Unauthorized", rec.Code)
	}
}

func TestCanonicalTargetRejection(t *testing.T) {
	tests := []string{
		"not-origin-form",
		"//double-slash",
		"/path\\with\\backslash",
		"/path\rwith\rreturn",
		"/path\nwith\nnewline",
		"/path\twith\ttab",
		"/path#with-fragment",
		"/api%2Fv1%2Fusers",
		"/api/v1/users?query;with=semicolon",
		"/api/./v1/users",
		"/api/../v1/users",
		"/api//v1/users",
		"/api/v1/users/",
	}

	for _, tt := range tests {
		t.Run("canonical_"+tt, func(t *testing.T) {
			if _, ok := canonicalTarget(tt); ok {
				t.Errorf("canonicalTarget(%q) should have returned false", tt)
			}
		})
	}
}
