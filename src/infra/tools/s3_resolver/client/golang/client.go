// Resolves virtual S3 URIs to direct physical storage coordinates for AWS S3 and GCP Cloud Storage.

package s3resolver

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	// DefaultResolverURL is the in-cluster service endpoint for the S3 URI resolver.
	DefaultResolverURL = "http://s3-gateway.s3-system.svc:10080/resolve"
	// DefaultTokenPath is the projection path for Kubernetes service account tokens.
	DefaultTokenPath = "/var/run/secrets/kubernetes.io/serviceaccount/token"
)

// ResolvedTarget contains the physical storage coordinates resolved for an S3 URI.
type ResolvedTarget struct {
	Mode        string `json:"mode"`
	URI         string `json:"uri"`
	Bucket      string `json:"bucket"`
	Key         string `json:"key"`
	EndpointURL string `json:"endpoint_url"`
	Region      string `json:"region"`
	AuthType    string `json:"auth_type"`
}

// IsDirect returns true if accessing storage directly without proxy hops.
func (t *ResolvedTarget) IsDirect() bool {
	return t.Mode == "direct" || t.Mode == "direct_federated"
}

// CleanEndpoint returns the endpoint hostname and port without the http(s) scheme.
func (t *ResolvedTarget) CleanEndpoint() string {
	endpoint := strings.TrimPrefix(t.EndpointURL, "https://")
	return strings.TrimPrefix(endpoint, "http://")
}

// Scheme returns "https" or "http" corresponding to EndpointURL.
func (t *ResolvedTarget) Scheme() string {
	if strings.HasPrefix(t.EndpointURL, "https://") {
		return "https"
	}
	return "http"
}

// Options configures an S3 URI resolver Client.
type Options struct {
	ResolverURL string
	TokenPath   string
	Timeout     time.Duration
}

// Client resolves virtual S3 URIs into physical storage coordinates.
type Client struct {
	resolverURL string
	tokenPath   string
	httpClient  *http.Client
	mu          sync.RWMutex
	cache       map[string]*ResolvedTarget
}

// NewClient returns an initialized S3 URI resolver client.
func NewClient(opts ...Options) *Client {
	c := &Client{
		resolverURL: DefaultResolverURL,
		tokenPath:   DefaultTokenPath,
		httpClient:  &http.Client{Timeout: 3 * time.Second},
		cache:       make(map[string]*ResolvedTarget),
	}
	if u := os.Getenv("S3_RESOLVER_URL"); u != "" {
		c.resolverURL = u
	}
	if len(opts) > 0 {
		if opt := opts[0]; opt.ResolverURL != "" {
			c.resolverURL = opt.ResolverURL
		}
		if opt := opts[0]; opt.TokenPath != "" {
			c.tokenPath = opt.TokenPath
		}
		if opt := opts[0]; opt.Timeout > 0 {
			c.httpClient.Timeout = opt.Timeout
		}
	}
	return c
}

func (c *Client) readToken() string {
	data, err := os.ReadFile(c.tokenPath)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// Resolve resolves a virtual S3 URI (s3://...) into physical coordinates.
func (c *Client) Resolve(ctx context.Context, uri string) (*ResolvedTarget, error) {
	if !strings.HasPrefix(uri, "s3://") {
		return nil, fmt.Errorf("uri must start with s3://: %q", uri)
	}

	c.mu.RLock()
	if cached, ok := c.cache[uri]; ok {
		c.mu.RUnlock()
		return cached, nil
	}
	c.mu.RUnlock()

	reqURL := fmt.Sprintf("%s?uri=%s", c.resolverURL, url.QueryEscape(uri))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return c.fallback(uri), nil
	}
	if token := c.readToken(); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := c.httpClient.Do(req)
	if resp != nil {
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			_ = resp.Body.Close()
			return nil, fmt.Errorf("s3 resolver authorization failed with status %d: unauthorized access for uri %q", resp.StatusCode, uri)
		}
	}
	if err != nil || resp.StatusCode != http.StatusOK {
		if resp != nil {
			_ = resp.Body.Close()
		}
		return c.fallback(uri), nil
	}
	defer resp.Body.Close()

	var payload struct {
		Mode        string `json:"mode"`
		URI         string `json:"uri"`
		Bucket      string `json:"bucket"`
		Key         string `json:"key"`
		EndpointURL string `json:"endpoint_url"`
		Region      string `json:"region"`
		Auth        struct {
			Type string `json:"type"`
		} `json:"auth"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return c.fallback(uri), nil
	}

	region := payload.Region
	if region == "" {
		region = "us-east-1"
	}
	authType := payload.Auth.Type
	if authType == "" {
		authType = "ambient_workload_identity"
	}

	target := &ResolvedTarget{
		Mode:        payload.Mode,
		URI:         payload.URI,
		Bucket:      payload.Bucket,
		Key:         payload.Key,
		EndpointURL: payload.EndpointURL,
		Region:      region,
		AuthType:    authType,
	}

	c.mu.Lock()
	c.cache[uri] = target
	c.mu.Unlock()

	return target, nil
}

func (c *Client) fallback(uri string) *ResolvedTarget {
	bucket, key, _ := strings.Cut(strings.TrimPrefix(uri, "s3://"), "/")
	endpoint := os.Getenv("AWS_ENDPOINT_URL_S3")
	if endpoint == "" {
		endpoint = os.Getenv("AWS_ENDPOINT_URL")
	}
	if endpoint == "" {
		endpoint = "http://s3-gateway.s3-system.svc:10080"
	}
	region := os.Getenv("AWS_REGION")
	if region == "" {
		region = "us-east-1"
	}
	return &ResolvedTarget{
		Mode:        "fallback",
		URI:         uri,
		Bucket:      bucket,
		Key:         key,
		EndpointURL: endpoint,
		Region:      region,
		AuthType:    "gateway_credential",
	}
}

var (
	defaultClientInstance *Client
	defaultClientOnce     sync.Once
)

// DefaultClient returns the singleton resolver client instance.
func DefaultClient() *Client {
	defaultClientOnce.Do(func() {
		defaultClientInstance = NewClient()
	})
	return defaultClientInstance
}

// Resolve resolves a virtual S3 URI using the default client singleton.
func Resolve(ctx context.Context, uri string) (*ResolvedTarget, error) {
	return DefaultClient().Resolve(ctx, uri)
}
