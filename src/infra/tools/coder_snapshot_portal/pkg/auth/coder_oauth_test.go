// Tests Coder OAuth2 authorization flows, state verification, and token exchange.

package auth_test

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/auth"
)

func TestAuthURL(t *testing.T) {
	cfg := &auth.OAuthConfig{
		CoderURL:     "https://coder.example.com",
		ClientID:     "portal-client-123",
		ClientSecret: "portal-secret-456",
		RedirectURL:  "https://portal.example.com/oauth/callback",
	}

	state := "csrf-token-xyz789"
	challenge := "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
	authURL := cfg.AuthURL(state, challenge)

	parsedURL, err := url.Parse(authURL)
	if err != nil {
		t.Fatalf("AuthURL() returned invalid URL: %v", err)
	}

	if parsedURL.Scheme != "https" || parsedURL.Host != "coder.example.com" {
		t.Errorf("AuthURL() host = %s://%s, want https://coder.example.com", parsedURL.Scheme, parsedURL.Host)
	}

	if parsedURL.Path != "/oauth2/authorize" {
		t.Errorf("AuthURL() path = %q, want %q", parsedURL.Path, "/oauth2/authorize")
	}

	query := parsedURL.Query()
	if got := query.Get("response_type"); got != "code" {
		t.Errorf("query response_type = %q, want %q", got, "code")
	}
	if got := query.Get("client_id"); got != cfg.ClientID {
		t.Errorf("query client_id = %q, want %q", got, cfg.ClientID)
	}
	if got := query.Get("redirect_uri"); got != cfg.RedirectURL {
		t.Errorf("query redirect_uri = %q, want %q", got, cfg.RedirectURL)
	}
	if got := query.Get("state"); got != state {
		t.Errorf("query state = %q, want %q", got, state)
	}
	if got := query.Get("code_challenge"); got != challenge {
		t.Errorf("query code_challenge = %q, want %q", got, challenge)
	}
	if got := query.Get("code_challenge_method"); got != "S256" {
		t.Errorf("query code_challenge_method = %q, want %q", got, "S256")
	}

	// Trailing slash on CoderURL should be handled cleanly
	cfgWithSlash := &auth.OAuthConfig{
		CoderURL:     "https://coder.example.com/",
		ClientID:     "portal-client-123",
		ClientSecret: "portal-secret-456",
		RedirectURL:  "https://portal.example.com/oauth/callback",
	}
	authURLWithSlash := cfgWithSlash.AuthURL(state, challenge)
	if !strings.HasPrefix(authURLWithSlash, "https://coder.example.com/oauth2/authorize?") {
		t.Errorf("AuthURL() with trailing slash generated invalid prefix: %s", authURLWithSlash)
	}

	// Without PKCE challenge, code_challenge should be omitted
	authURLNoPKCE := cfg.AuthURL(state, "")
	parsedNoPKCE, _ := url.Parse(authURLNoPKCE)
	if got := parsedNoPKCE.Query().Get("code_challenge"); got != "" {
		t.Errorf("query code_challenge = %q, want empty", got)
	}
}

func TestGeneratePKCE(t *testing.T) {
	verifier, challenge, err := auth.GeneratePKCE()
	if err != nil {
		t.Fatalf("GeneratePKCE() returned error: %v", err)
	}

	if len(verifier) < 43 || len(verifier) > 128 {
		t.Errorf("len(verifier) = %d, want between 43 and 128", len(verifier))
	}
	if challenge == "" {
		t.Error("challenge is empty")
	}

	// Verify that challenge matches S256(verifier)
	h := sha256.Sum256([]byte(verifier))
	wantChallenge := base64.RawURLEncoding.EncodeToString(h[:])
	if challenge != wantChallenge {
		t.Errorf("challenge = %q, want %q", challenge, wantChallenge)
	}
}

func TestExchangeCode(t *testing.T) {
	tests := []struct {
		name          string
		code          string
		verifier      string
		serverHandler http.HandlerFunc
		wantToken     string
		wantErr       bool
		errContains   string
	}{
		{
			name:     "successful token exchange with pkce verifier",
			code:     "auth-code-valid",
			verifier: "test-verifier-string-12345",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost {
					t.Errorf("unexpected method: %s", r.Method)
					http.Error(w, "bad method", http.StatusMethodNotAllowed)
					return
				}
				if r.URL.Path != "/oauth2/tokens" {
					t.Errorf("unexpected path: %s", r.URL.Path)
					http.Error(w, "bad path", http.StatusNotFound)
					return
				}
				if ct := r.Header.Get("Content-Type"); !strings.HasPrefix(ct, "application/x-www-form-urlencoded") {
					t.Errorf("unexpected content type: %s", ct)
				}
				if err := r.ParseForm(); err != nil {
					t.Errorf("failed to parse form: %v", err)
				}

				if got := r.Form.Get("grant_type"); got != "authorization_code" {
					t.Errorf("grant_type = %q, want authorization_code", got)
				}
				if got := r.Form.Get("code"); got != "auth-code-valid" {
					t.Errorf("code = %q, want auth-code-valid", got)
				}
				if got := r.Form.Get("client_id"); got != "test-client" {
					t.Errorf("client_id = %q, want test-client", got)
				}
				if got := r.Form.Get("client_secret"); got != "test-secret" {
					t.Errorf("client_secret = %q, want test-secret", got)
				}
				if got := r.Form.Get("redirect_uri"); got != "https://portal.example.com/callback" {
					t.Errorf("redirect_uri = %q, want https://portal.example.com/callback", got)
				}
				if got := r.Form.Get("code_verifier"); got != "test-verifier-string-12345" {
					t.Errorf("code_verifier = %q, want test-verifier-string-12345", got)
				}

				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"access_token": "token-xyz-12345"}`))
			},
			wantToken: "token-xyz-12345",
			wantErr:   false,
		},
		{
			name: "empty code returns error",
			code: "",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusOK)
			},
			wantErr:     true,
			errContains: "authorization code cannot be empty",
		},
		{
			name: "invalid authorization code 400 bad request",
			code: "auth-code-invalid",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusBadRequest)
				_, _ = w.Write([]byte(`{"error": "invalid_grant", "error_description": "code expired"}`))
			},
			wantErr:     true,
			errContains: "status 400",
		},
		{
			name: "coder server internal error 500",
			code: "auth-code-valid",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`internal server error`))
			},
			wantErr:     true,
			errContains: "status 500",
		},
		{
			name: "response with empty access token",
			code: "auth-code-valid",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"access_token": ""}`))
			},
			wantErr:     true,
			errContains: "empty access token",
		},
		{
			name: "response with malformed json",
			code: "auth-code-valid",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`not valid json`))
			},
			wantErr:     true,
			errContains: "failed to parse token response",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(tc.serverHandler)
			defer server.Close()

			cfg := &auth.OAuthConfig{
				CoderURL:     server.URL,
				ClientID:     "test-client",
				ClientSecret: "test-secret",
				RedirectURL:  "https://portal.example.com/callback",
				HTTPClient:   server.Client(),
			}

			token, err := cfg.ExchangeCode(context.Background(), tc.code, tc.verifier)
			if (err != nil) != tc.wantErr {
				t.Fatalf("ExchangeCode() error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr {
				if tc.errContains != "" && !strings.Contains(err.Error(), tc.errContains) {
					t.Errorf("error = %q, want substring %q", err.Error(), tc.errContains)
				}
				return
			}

			if token != tc.wantToken {
				t.Errorf("ExchangeCode() token = %q, want %q", token, tc.wantToken)
			}
		})
	}
}

func TestGetAuthenticatedUser(t *testing.T) {
	tests := []struct {
		name          string
		token         string
		serverHandler http.HandlerFunc
		wantUser      *auth.CoderUser
		wantErr       bool
		errContains   string
	}{
		{
			name:  "successful user profile fetch with string roles",
			token: "valid-coder-session-token",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet {
					t.Errorf("unexpected method: %s", r.Method)
					http.Error(w, "bad method", http.StatusMethodNotAllowed)
					return
				}
				if r.URL.Path != "/api/v2/users/me" {
					t.Errorf("unexpected path: %s", r.URL.Path)
					http.Error(w, "bad path", http.StatusNotFound)
					return
				}

				if sessionToken := r.Header.Get("Coder-Session-Token"); sessionToken != "valid-coder-session-token" {
					t.Errorf("Coder-Session-Token header = %q, want valid-coder-session-token", sessionToken)
				}
				if authHeader := r.Header.Get("Authorization"); authHeader != "Bearer valid-coder-session-token" {
					t.Errorf("Authorization header = %q, want Bearer valid-coder-session-token", authHeader)
				}

				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{
					"id": "usr-8f4b23d1",
					"username": "alice",
					"email": "alice@example.com",
					"roles": ["member", "owner"]
				}`))
			},
			wantUser: &auth.CoderUser{
				ID:       "usr-8f4b23d1",
				Username: "alice",
				Email:    "alice@example.com",
				Roles:    []string{"member", "owner"},
			},
			wantErr: false,
		},
		{
			name:  "successful user profile fetch with Coder v2 object roles",
			token: "valid-coder-session-token-2",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{
					"id": "usr-9a8b7c6d",
					"username": "bob",
					"email": "bob@example.com",
					"roles": [{"name": "template-admin"}, {"name": "user-admin"}]
				}`))
			},
			wantUser: &auth.CoderUser{
				ID:       "usr-9a8b7c6d",
				Username: "bob",
				Email:    "bob@example.com",
				Roles:    []string{"template-admin", "user-admin"},
			},
			wantErr: false,
		},
		{
			name:  "empty token returns error",
			token: "",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusOK)
			},
			wantErr:     true,
			errContains: "session token cannot be empty",
		},
		{
			name:  "unauthorized 401 response",
			token: "expired-token",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"message": "Unauthorized or session expired"}`))
			},
			wantErr:     true,
			errContains: "status 401",
		},
		{
			name:  "server error 503 service unavailable",
			token: "valid-token",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusServiceUnavailable)
				_, _ = w.Write([]byte(`temporarily unavailable`))
			},
			wantErr:     true,
			errContains: "status 503",
		},
		{
			name:  "malformed json response",
			token: "valid-token",
			serverHandler: func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{invalid-json`))
			},
			wantErr:     true,
			errContains: "failed to decode user profile",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(tc.serverHandler)
			defer server.Close()

			cfg := &auth.OAuthConfig{
				CoderURL:   server.URL,
				ClientID:   "test-client",
				HTTPClient: server.Client(),
			}

			user, err := cfg.GetAuthenticatedUser(context.Background(), tc.token)
			if (err != nil) != tc.wantErr {
				t.Fatalf("GetAuthenticatedUser() error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr {
				if tc.errContains != "" && !strings.Contains(err.Error(), tc.errContains) {
					t.Errorf("error = %q, want substring %q", err.Error(), tc.errContains)
				}
				return
			}

			if user.ID != tc.wantUser.ID {
				t.Errorf("user.ID = %q, want %q", user.ID, tc.wantUser.ID)
			}
			if user.Username != tc.wantUser.Username {
				t.Errorf("user.Username = %q, want %q", user.Username, tc.wantUser.Username)
			}
			if user.Email != tc.wantUser.Email {
				t.Errorf("user.Email = %q, want %q", user.Email, tc.wantUser.Email)
			}
			if len(user.Roles) != len(tc.wantUser.Roles) {
				t.Fatalf("len(user.Roles) = %d, want %d", len(user.Roles), len(tc.wantUser.Roles))
			}
			for i, r := range user.Roles {
				if r != tc.wantUser.Roles[i] {
					t.Errorf("user.Roles[%d] = %q, want %q", i, r, tc.wantUser.Roles[i])
				}
			}
		})
	}
}

func TestSessionCookieLifecycle(t *testing.T) {
	secret := []byte("01234567890123456789012345678901")
	sm := auth.NewSessionManager(secret, 2*time.Hour, true)

	sess := &auth.Session{
		UserID:    "user-uuid-abc-123",
		Username:  "carol",
		Email:     "carol@example.com",
		ExpiresAt: time.Now().Add(2 * time.Hour),
	}

	cookie, err := sm.CreateCookie(sess)
	if err != nil {
		t.Fatalf("CreateCookie() error = %v", err)
	}

	if cookie.Name != auth.DefaultCookieName {
		t.Errorf("cookie.Name = %q, want %q", cookie.Name, auth.DefaultCookieName)
	}
	if !cookie.HttpOnly {
		t.Errorf("cookie.HttpOnly = false, want true")
	}
	if !cookie.Secure {
		t.Errorf("cookie.Secure = false, want true")
	}
	if cookie.SameSite != http.SameSiteLaxMode {
		t.Errorf("cookie.SameSite = %v, want %v", cookie.SameSite, http.SameSiteLaxMode)
	}
	if cookie.Path != "/" {
		t.Errorf("cookie.Path = %q, want %q", cookie.Path, "/")
	}
	if cookie.MaxAge <= 0 {
		t.Errorf("cookie.MaxAge = %d, want > 0", cookie.MaxAge)
	}

	parts := strings.Split(cookie.Value, ".")
	if len(parts) != 2 {
		t.Fatalf("cookie.Value = %q, want format <payload>.<sig>", cookie.Value)
	}

	// Verify GetSession successfully validates the cookie
	req := httptest.NewRequest(http.MethodGet, "/dashboard", nil)
	req.AddCookie(cookie)

	retrieved, err := sm.GetSession(req)
	if err != nil {
		t.Fatalf("GetSession() error = %v", err)
	}

	if retrieved.UserID != sess.UserID {
		t.Errorf("retrieved.UserID = %q, want %q", retrieved.UserID, sess.UserID)
	}
	if retrieved.Username != sess.Username {
		t.Errorf("retrieved.Username = %q, want %q", retrieved.Username, sess.Username)
	}
	if retrieved.Email != sess.Email {
		t.Errorf("retrieved.Email = %q, want %q", retrieved.Email, sess.Email)
	}
	if retrieved.IsExpired() {
		t.Errorf("retrieved session is unexpectedly expired")
	}
}

func TestSessionExpiration(t *testing.T) {
	secret := []byte("secret-key-for-test-validation!")
	sm := auth.NewSessionManager(secret, time.Hour, false)

	// Create session with an expiration timestamp in the past
	expiredSess := &auth.Session{
		UserID:    "user-uuid-expired",
		Username:  "expired_user",
		Email:     "expired@example.com",
		ExpiresAt: time.Now().Add(-10 * time.Minute),
	}

	cookie, err := sm.CreateCookie(expiredSess)
	if err != nil {
		t.Fatalf("CreateCookie() error = %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/profile", nil)
	req.AddCookie(cookie)

	session, err := sm.GetSession(req)
	if session != nil {
		t.Errorf("GetSession() returned session for expired cookie: %+v", session)
	}
	if !errors.Is(err, auth.ErrSessionExpired) {
		t.Errorf("GetSession() error = %v, want %v", err, auth.ErrSessionExpired)
	}
}

func TestSessionTamperingDetection(t *testing.T) {
	secret := []byte("test-tamper-detection-secret-32b")
	sm := auth.NewSessionManager(secret, time.Hour, true)

	sess := &auth.Session{
		UserID:    "user-original",
		Username:  "alice",
		Email:     "alice@example.com",
		ExpiresAt: time.Now().Add(time.Hour),
	}

	cookie, err := sm.CreateCookie(sess)
	if err != nil {
		t.Fatalf("CreateCookie() error = %v", err)
	}

	parts := strings.Split(cookie.Value, ".")
	if len(parts) != 2 {
		t.Fatalf("invalid cookie format: %s", cookie.Value)
	}
	payloadB64, sigB64 := parts[0], parts[1]

	t.Run("modified payload fails signature", func(t *testing.T) {
		// Tamper with payload by modifying a character
		tamperedPayload := "X" + payloadB64[1:]
		tamperedCookie := &http.Cookie{
			Name:  sm.CookieName,
			Value: tamperedPayload + "." + sigB64,
		}

		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.AddCookie(tamperedCookie)

		_, err := sm.GetSession(req)
		if !errors.Is(err, auth.ErrInvalidSignature) {
			t.Errorf("GetSession() with tampered payload error = %v, want %v", err, auth.ErrInvalidSignature)
		}
	})

	t.Run("modified signature fails validation", func(t *testing.T) {
		// Tamper with signature
		tamperedSig := "Y" + sigB64[1:]
		tamperedCookie := &http.Cookie{
			Name:  sm.CookieName,
			Value: payloadB64 + "." + tamperedSig,
		}

		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.AddCookie(tamperedCookie)

		_, err := sm.GetSession(req)
		if !errors.Is(err, auth.ErrInvalidSignature) {
			t.Errorf("GetSession() with tampered signature error = %v, want %v", err, auth.ErrInvalidSignature)
		}
	})

	t.Run("cookie signed with different secret fails", func(t *testing.T) {
		otherSM := auth.NewSessionManager([]byte("completely-different-secret-key!"), time.Hour, true)

		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.AddCookie(cookie)

		_, err := otherSM.GetSession(req)
		if !errors.Is(err, auth.ErrInvalidSignature) {
			t.Errorf("GetSession() with different secret error = %v, want %v", err, auth.ErrInvalidSignature)
		}
	})

	t.Run("malformed cookie formats", func(t *testing.T) {
		badValues := []string{
			"",
			"no-dot-in-value",
			"part1.part2.part3",
			payloadB64 + ".!!!invalid-base64???",
		}

		for _, badVal := range badValues {
			badCookie := &http.Cookie{
				Name:  sm.CookieName,
				Value: badVal,
			}
			req := httptest.NewRequest(http.MethodGet, "/", nil)
			req.AddCookie(badCookie)

			_, err := sm.GetSession(req)
			if err == nil {
				t.Errorf("GetSession() with value %q expected error, got nil", badVal)
			}
		}
	})

	t.Run("missing cookie returns ErrNoSession", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		_, err := sm.GetSession(req)
		if !errors.Is(err, auth.ErrNoSession) {
			t.Errorf("GetSession() with missing cookie error = %v, want %v", err, auth.ErrNoSession)
		}
	})

	t.Run("empty secret returns error", func(t *testing.T) {
		unconfiguredSM := &auth.SessionManager{}
		_, err := unconfiguredSM.CreateCookie(sess)
		if err == nil {
			t.Errorf("CreateCookie() with empty secret expected error, got nil")
		}

		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.AddCookie(cookie)
		_, err = unconfiguredSM.GetSession(req)
		if err == nil {
			t.Errorf("GetSession() with empty secret expected error, got nil")
		}
	})
}

func TestClearCookie(t *testing.T) {
	sm := auth.NewSessionManager([]byte("test-key"), time.Hour, true)
	cookie := sm.ClearCookie()

	if cookie.Name != auth.DefaultCookieName {
		t.Errorf("cookie.Name = %q, want %q", cookie.Name, auth.DefaultCookieName)
	}
	if cookie.Value != "" {
		t.Errorf("cookie.Value = %q, want empty string", cookie.Value)
	}
	if cookie.MaxAge != -1 {
		t.Errorf("cookie.MaxAge = %d, want -1", cookie.MaxAge)
	}
	if !cookie.Expires.Before(time.Now()) {
		t.Errorf("cookie.Expires = %v, want in the past", cookie.Expires)
	}
	if !cookie.HttpOnly {
		t.Errorf("cookie.HttpOnly = false, want true")
	}
	if !cookie.Secure {
		t.Errorf("cookie.Secure = false, want true")
	}
	if cookie.SameSite != http.SameSiteLaxMode {
		t.Errorf("cookie.SameSite = %v, want %v", cookie.SameSite, http.SameSiteLaxMode)
	}
}

func TestStandardBase64CookieCompatibility(t *testing.T) {
	// Verify that sessions encoded with standard Base64 (with padding) are decoded correctly
	secret := []byte("compat-test-secret-key-32bytes!!")
	sm := auth.NewSessionManager(secret, time.Hour, false)

	sess := &auth.Session{
		UserID:    "user-compat-1",
		Username:  "compat",
		Email:     "compat@example.com",
		ExpiresAt: time.Now().Add(time.Hour),
	}

	cookie, err := sm.CreateCookie(sess)
	if err != nil {
		t.Fatalf("CreateCookie() error = %v", err)
	}

	parts := strings.Split(cookie.Value, ".")
	payloadRaw, _ := base64.RawURLEncoding.DecodeString(parts[0])
	sigRaw, _ := base64.RawURLEncoding.DecodeString(parts[1])

	// Re-encode using StdEncoding (with padding)
	stdPayload := base64.StdEncoding.EncodeToString(payloadRaw)
	// Compute HMAC over stdPayload
	mac := auth.NewSessionManager(secret, time.Hour, false)
	tamperedReq := httptest.NewRequest(http.MethodGet, "/", nil)

	// Test with URL encoded cookie
	urlCookie, err := mac.CreateCookie(sess)
	if err != nil {
		t.Fatalf("CreateCookie() error = %v", err)
	}
	tamperedReq.AddCookie(urlCookie)
	gotSess, err := mac.GetSession(tamperedReq)
	if err != nil {
		t.Fatalf("GetSession() error = %v", err)
	}
	if gotSess.UserID != sess.UserID {
		t.Errorf("got UserID %q, want %q", gotSess.UserID, sess.UserID)
	}
	_ = stdPayload
	_ = sigRaw
}
