// Manages HMAC-signed session cookies and cryptographic verification for user sessions.

package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// DefaultCookieName is the default cookie name for signed session cookies.
const DefaultCookieName = "coder_snapshot_session"

var (
	// ErrNoSession is returned when the session cookie is not present in the HTTP request.
	ErrNoSession = errors.New("auth: session cookie not found")

	// ErrInvalidCookie is returned when the cookie format or encoding is malformed.
	ErrInvalidCookie = errors.New("auth: invalid session cookie format")

	// ErrInvalidSignature is returned when the HMAC signature verification fails.
	ErrInvalidSignature = errors.New("auth: invalid session signature")

	// ErrSessionExpired is returned when the session has passed its expiration time.
	ErrSessionExpired = errors.New("auth: session expired")
)

// Session holds authenticated session state stored securely in a signed browser cookie.
type Session struct {
	UserID    string    `json:"user_id"`
	Username  string    `json:"username"`
	Email     string    `json:"email"`
	ExpiresAt time.Time `json:"expires_at"`
}

// IsExpired returns true if the session is past its expiration time.
func (s *Session) IsExpired() bool {
	if s == nil || s.ExpiresAt.IsZero() {
		return false
	}
	return time.Now().After(s.ExpiresAt)
}

// SessionManager manages signed session cookie lifecycle and verification.
type SessionManager struct {
	CookieName string
	Secret     []byte
	TTL        time.Duration
	Secure     bool
}

// NewSessionManager creates a new SessionManager initialized with default cookie parameters.
func NewSessionManager(secret []byte, ttl time.Duration, secure bool) *SessionManager {
	return &SessionManager{
		CookieName: DefaultCookieName,
		Secret:     secret,
		TTL:        ttl,
		Secure:     secure,
	}
}

func (m *SessionManager) cookieName() string {
	if m.CookieName != "" {
		return m.CookieName
	}
	return DefaultCookieName
}

// CreateCookie serializes session to JSON, base64 encodes it, computes HMAC-SHA256 signature,
// formats as <base64Payload>.<base64Signature>, and returns an http.Cookie with HttpOnly and SameSite=Lax.
func (m *SessionManager) CreateCookie(session *Session) (*http.Cookie, error) {
	if session == nil {
		return nil, errors.New("auth: session cannot be nil")
	}
	if len(m.Secret) == 0 {
		return nil, errors.New("auth: session secret cannot be empty")
	}

	sessCopy := *session
	if sessCopy.ExpiresAt.IsZero() {
		ttl := m.TTL
		if ttl <= 0 {
			ttl = 24 * time.Hour
		}
		sessCopy.ExpiresAt = time.Now().Add(ttl)
	}

	payloadJSON, err := json.Marshal(sessCopy)
	if err != nil {
		return nil, fmt.Errorf("auth: failed to serialize session: %w", err)
	}

	payloadB64 := base64.RawURLEncoding.EncodeToString(payloadJSON)

	mac := hmac.New(sha256.New, m.Secret)
	mac.Write([]byte(payloadB64))
	sig := mac.Sum(nil)
	sigB64 := base64.RawURLEncoding.EncodeToString(sig)

	cookieValue := payloadB64 + "." + sigB64

	cookie := &http.Cookie{
		Name:     m.cookieName(),
		Value:    cookieValue,
		Path:     "/",
		HttpOnly: true,
		Secure:   m.Secure,
		SameSite: http.SameSiteLaxMode,
		Expires:  sessCopy.ExpiresAt,
	}

	maxAge := int(time.Until(sessCopy.ExpiresAt).Seconds())
	if maxAge > 0 {
		cookie.MaxAge = maxAge
	}

	return cookie, nil
}

// GetSession reads the session cookie, verifies signature with constant-time compare (hmac.Equal),
// checks expiration, and deserializes the JSON payload into a Session.
func (m *SessionManager) GetSession(r *http.Request) (*Session, error) {
	if r == nil {
		return nil, errors.New("auth: request cannot be nil")
	}
	if len(m.Secret) == 0 {
		return nil, errors.New("auth: session secret cannot be empty")
	}

	cookie, err := r.Cookie(m.cookieName())
	if err != nil {
		return nil, ErrNoSession
	}

	parts := strings.Split(cookie.Value, ".")
	if len(parts) != 2 {
		return nil, ErrInvalidCookie
	}

	payloadB64 := parts[0]
	sigB64 := parts[1]

	sig, err := decodeBase64(sigB64)
	if err != nil {
		return nil, ErrInvalidCookie
	}

	// Verify HMAC-SHA256 signature using constant-time comparison
	mac := hmac.New(sha256.New, m.Secret)
	mac.Write([]byte(payloadB64))
	expectedSig := mac.Sum(nil)

	valid := hmac.Equal(sig, expectedSig)
	if !valid {
		// Also verify against raw decoded JSON payload bytes in case signature was computed over raw JSON
		if rawBytes, rawErr := decodeBase64(payloadB64); rawErr == nil {
			macRaw := hmac.New(sha256.New, m.Secret)
			macRaw.Write(rawBytes)
			if hmac.Equal(sig, macRaw.Sum(nil)) {
				valid = true
			}
		}
	}

	if !valid {
		return nil, ErrInvalidSignature
	}

	payloadBytes, err := decodeBase64(payloadB64)
	if err != nil {
		return nil, ErrInvalidCookie
	}

	var session Session
	if err := json.Unmarshal(payloadBytes, &session); err != nil {
		return nil, fmt.Errorf("auth: failed to deserialize session payload: %w", err)
	}

	if !session.ExpiresAt.IsZero() && time.Now().After(session.ExpiresAt) {
		return nil, ErrSessionExpired
	}

	return &session, nil
}

// ClearCookie returns an expired HTTP cookie configured to clear the browser session.
func (m *SessionManager) ClearCookie() *http.Cookie {
	return &http.Cookie{
		Name:     m.cookieName(),
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   m.Secure,
		SameSite: http.SameSiteLaxMode,
		Expires:  time.Unix(0, 0),
		MaxAge:   -1,
	}
}

// decodeBase64 attempts decoding standard, URL-safe, padded and unpadded Base64 variants.
func decodeBase64(s string) ([]byte, error) {
	if b, err := base64.RawURLEncoding.DecodeString(s); err == nil {
		return b, nil
	}
	if b, err := base64.URLEncoding.DecodeString(s); err == nil {
		return b, nil
	}
	if b, err := base64.RawStdEncoding.DecodeString(s); err == nil {
		return b, nil
	}
	return base64.StdEncoding.DecodeString(s)
}
