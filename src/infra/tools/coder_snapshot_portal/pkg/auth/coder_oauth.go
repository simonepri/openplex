// Authenticates users against Coder via OAuth2 authorization code flow and token exchange.

package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// OAuthConfig holds the OAuth2 configuration for authenticating with Coder.
type OAuthConfig struct {
	CoderURL     string
	ClientID     string
	ClientSecret string
	RedirectURL  string
	HTTPClient   *http.Client
}

// CoderUser represents the profile of an authenticated Coder user.
type CoderUser struct {
	ID       string   `json:"id"`
	Username string   `json:"username"`
	Email    string   `json:"email"`
	Roles    []string `json:"roles"`
}

// UnmarshalJSON implements custom JSON unmarshaling for CoderUser to handle
// both string role lists and Coder v2 API object role representations.
func (u *CoderUser) UnmarshalJSON(data []byte) error {
	type rawCoderUser struct {
		ID       string          `json:"id"`
		Username string          `json:"username"`
		Email    string          `json:"email"`
		Roles    json.RawMessage `json:"roles"`
	}

	var raw rawCoderUser
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}

	u.ID = raw.ID
	u.Username = raw.Username
	u.Email = raw.Email

	if len(raw.Roles) > 0 {
		var strRoles []string
		if err := json.Unmarshal(raw.Roles, &strRoles); err == nil {
			u.Roles = strRoles
			return nil
		}

		var objRoles []struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(raw.Roles, &objRoles); err == nil {
			u.Roles = make([]string, len(objRoles))
			for i, r := range objRoles {
				u.Roles[i] = r.Name
			}
			return nil
		}
	}

	return nil
}

// GeneratePKCE generates an RFC 7636 high-entropy code verifier and S256 challenge.
func GeneratePKCE() (verifier, challenge string, err error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", "", fmt.Errorf("auth: failed to generate pkce entropy: %w", err)
	}
	verifier = base64.RawURLEncoding.EncodeToString(raw)
	h := sha256.Sum256([]byte(verifier))
	challenge = base64.RawURLEncoding.EncodeToString(h[:])
	return verifier, challenge, nil
}

// AuthURL returns the Coder OAuth2 authorization URL with the required query parameters.
func (c *OAuthConfig) AuthURL(state, codeChallenge string) string {
	baseURL := strings.TrimRight(c.CoderURL, "/") + "/oauth2/authorize"
	u, err := url.Parse(baseURL)
	if err != nil {
		return ""
	}

	q := u.Query()
	q.Set("response_type", "code")
	q.Set("client_id", c.ClientID)
	q.Set("redirect_uri", c.RedirectURL)
	if state != "" {
		q.Set("state", state)
	}
	if codeChallenge != "" {
		q.Set("code_challenge", codeChallenge)
		q.Set("code_challenge_method", "S256")
	}
	u.RawQuery = q.Encode()

	return u.String()
}

// ExchangeCode exchanges an authorization code and PKCE verifier for an OAuth2 access token.
func (c *OAuthConfig) ExchangeCode(ctx context.Context, code, codeVerifier string) (string, error) {
	if code == "" {
		return "", errors.New("auth: authorization code cannot be empty")
	}

	tokenEndpoint := strings.TrimRight(c.CoderURL, "/") + "/oauth2/tokens"

	form := url.Values{}
	form.Set("grant_type", "authorization_code")
	form.Set("code", code)
	form.Set("client_id", c.ClientID)
	form.Set("client_secret", c.ClientSecret)
	form.Set("redirect_uri", c.RedirectURL)
	if codeVerifier != "" {
		form.Set("code_verifier", codeVerifier)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenEndpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("auth: failed to create token request: %w", err)
	}

	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	client := c.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}

	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("auth: token exchange request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("auth: failed to read token response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("auth: token exchange failed with status %d: %s", resp.StatusCode, string(body))
	}

	var tokenResp struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.Unmarshal(body, &tokenResp); err != nil {
		return "", fmt.Errorf("auth: failed to parse token response: %w", err)
	}

	if tokenResp.AccessToken == "" {
		return "", errors.New("auth: empty access token in response")
	}

	return tokenResp.AccessToken, nil
}

// GetAuthenticatedUser fetches the authenticated CoderUser profile from Coder API.
func (c *OAuthConfig) GetAuthenticatedUser(ctx context.Context, token string) (*CoderUser, error) {
	if token == "" {
		return nil, errors.New("auth: session token cannot be empty")
	}

	userEndpoint := strings.TrimRight(c.CoderURL, "/") + "/api/v2/users/me"

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, userEndpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("auth: failed to create user profile request: %w", err)
	}

	req.Header.Set("Coder-Session-Token", token)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")

	client := c.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("auth: user profile request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("auth: failed to read user profile response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("auth: user profile request failed with status %d: %s", resp.StatusCode, string(body))
	}

	var user CoderUser
	if err := json.Unmarshal(body, &user); err != nil {
		return nil, fmt.Errorf("auth: failed to decode user profile: %w", err)
	}

	return &user, nil
}
