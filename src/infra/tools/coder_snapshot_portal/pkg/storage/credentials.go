// Implements AWS credential sources for SigV4 request signing.

package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// containerCredentialsRefreshWindow is how long before expiry cached container credentials are replaced.
const containerCredentialsRefreshWindow = 5 * time.Minute

// Credentials holds one set of AWS signing credentials.
type Credentials struct {
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
}

// CredentialsProvider returns credentials valid for signing a request now.
type CredentialsProvider interface {
	Retrieve(ctx context.Context) (Credentials, error)
}

// StaticCredentials signs every request with the same long-lived credentials.
type StaticCredentials Credentials

// Retrieve returns the fixed credentials.
func (c StaticCredentials) Retrieve(context.Context) (Credentials, error) {
	return Credentials(c), nil
}

// ContainerCredentials fetches temporary credentials from a container credentials endpoint,
// such as the EKS Pod Identity agent, and caches them until shortly before they expire.
// It is safe for concurrent use.
type ContainerCredentials struct {
	URI        string
	TokenFile  string
	HTTPClient *http.Client

	mu      sync.Mutex
	cached  Credentials
	expires time.Time
}

// Retrieve returns cached credentials, fetching new ones when they are close to expiry.
func (c *ContainerCredentials) Retrieve(ctx context.Context) (Credentials, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if time.Until(c.expires) > containerCredentialsRefreshWindow {
		return c.cached, nil
	}

	creds, expires, err := c.fetch(ctx)
	if err != nil {
		return Credentials{}, err
	}
	c.cached, c.expires = creds, expires
	return creds, nil
}

func (c *ContainerCredentials) fetch(ctx context.Context) (Credentials, time.Time, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.URI, nil)
	if err != nil {
		return Credentials{}, time.Time{}, err
	}

	// The token file is rotated by the kubelet, so read it on every fetch.
	if c.TokenFile != "" {
		token, err := os.ReadFile(c.TokenFile)
		if err != nil {
			return Credentials{}, time.Time{}, fmt.Errorf("read container credentials token: %w", err)
		}
		req.Header.Set("Authorization", strings.TrimSpace(string(token)))
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return Credentials{}, time.Time{}, fmt.Errorf("fetch container credentials: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return Credentials{}, time.Time{}, fmt.Errorf("container credentials endpoint returned status %d: %s", resp.StatusCode, string(body))
	}

	var payload struct {
		AccessKeyID     string    `json:"AccessKeyId"`
		SecretAccessKey string    `json:"SecretAccessKey"`
		Token           string    `json:"Token"`
		Expiration      time.Time `json:"Expiration"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return Credentials{}, time.Time{}, fmt.Errorf("decode container credentials: %w", err)
	}

	creds := Credentials{
		AccessKeyID:     payload.AccessKeyID,
		SecretAccessKey: payload.SecretAccessKey,
		SessionToken:    payload.Token,
	}
	return creds, payload.Expiration, nil
}
