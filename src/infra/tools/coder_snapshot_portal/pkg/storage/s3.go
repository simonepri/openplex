// Implements S3-compatible snapshot storage with AWS SigV4 signing and pagination.

package storage

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
)

var (
	validUserID   = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$`)
	validSelector = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
)

// S3Config holds configuration options for S3 storage access.
type S3Config struct {
	Endpoint        string
	Bucket          string
	Region          string
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
	UsePathStyle    bool
	KeyPrefix       string
	HTTPClient      *http.Client
}

// S3Store provides direct S3 storage access for snapshot manifests without external SDKs.
type S3Store struct {
	cfg        S3Config
	baseURL    *url.URL
	httpClient *http.Client
}

type listObjectsV2Output struct {
	XMLName               xml.Name       `xml:"ListBucketResult"`
	Name                  string         `xml:"Name"`
	Prefix                string         `xml:"Prefix"`
	KeyCount              int            `xml:"KeyCount"`
	MaxKeys               int            `xml:"MaxKeys"`
	IsTruncated           bool           `xml:"IsTruncated"`
	Contents              []s3Object     `xml:"Contents"`
	CommonPrefixes        []commonPrefix `xml:"CommonPrefixes"`
	NextContinuationToken string         `xml:"NextContinuationToken"`
}

type commonPrefix struct {
	Prefix string `xml:"Prefix"`
}

type s3Object struct {
	Key          string    `xml:"Key"`
	LastModified time.Time `xml:"LastModified"`
	Size         int64     `xml:"Size"`
}

// NewS3Store initializes an S3Store with standard HTTP client and SigV4 signer.
func NewS3Store(cfg S3Config) (*S3Store, error) {
	if cfg.Bucket == "" {
		return nil, fmt.Errorf("s3 bucket is required")
	}

	endpoint := cfg.Endpoint
	if endpoint == "" {
		endpoint = "https://s3.amazonaws.com"
	} else if !strings.HasPrefix(endpoint, "http://") && !strings.HasPrefix(endpoint, "https://") {
		endpoint = "https://" + endpoint
	}

	parsedURL, err := url.Parse(endpoint)
	if err != nil {
		return nil, fmt.Errorf("invalid s3 endpoint URL %q: %w", endpoint, err)
	}

	if cfg.Region == "" {
		cfg.Region = "us-east-1"
	}

	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}

	return &S3Store{
		cfg:        cfg,
		baseURL:    parsedURL,
		httpClient: client,
	}, nil
}

// candidatePrefixes returns all potential S3 key prefixes where user snapshots may reside.
func (s *S3Store) candidatePrefixes(ctx context.Context, userID string) []string {
	seen := make(map[string]bool)
	var prefixes []string

	add := func(p string) {
		if p != "" && !seen[p] {
			seen[p] = true
			prefixes = append(prefixes, p)
		}
	}

	// 1. Root user prefix
	add(fmt.Sprintf("owners/%s/snapshots/", userID))

	// 2. Explicitly configured key prefix
	if s.cfg.KeyPrefix != "" {
		p := strings.TrimSuffix(s.cfg.KeyPrefix, "/") + "/"
		add(fmt.Sprintf("%sowners/%s/snapshots/", p, userID))
		add(fmt.Sprintf("%srepos/%s/owners/%s/snapshots/", p, userID, userID))
		add(fmt.Sprintf("%srepos/%s/snapshots/", p, userID))
	}

	// 3. Dynamically discover team backup prefixes under backups/dev/
	teamOutput, err := s.listObjectsPage(ctx, "backups/dev/", "", "/")
	if err == nil && teamOutput != nil {
		for _, cp := range teamOutput.CommonPrefixes {
			add(fmt.Sprintf("%srepos/%s/owners/%s/snapshots/", cp.Prefix, userID, userID))
			add(fmt.Sprintf("%srepos/%s/snapshots/", cp.Prefix, userID))
		}
	}

	return prefixes
}

func (s *S3Store) listKeysForPrefix(ctx context.Context, prefix string) ([]string, error) {
	var keys []string
	continuationToken := ""

	for {
		output, err := s.listObjectsPage(ctx, prefix, continuationToken, "")
		if err != nil {
			return nil, fmt.Errorf("list objects failed: %w", err)
		}

		for _, item := range output.Contents {
			if strings.HasPrefix(item.Key, prefix) && strings.HasSuffix(item.Key, ".json") {
				keys = append(keys, item.Key)
			}
		}

		if !output.IsTruncated || output.NextContinuationToken == "" {
			break
		}
		continuationToken = output.NextContinuationToken
	}
	return keys, nil
}

// ListUserSnapshots queries all snapshot manifests for a user under owners/{userID}/snapshots/*.json
// and discovered backup prefixes.
func (s *S3Store) ListUserSnapshots(ctx context.Context, userID string) ([]*model.SnapshotManifest, error) {
	if err := validateUserID(userID); err != nil {
		return nil, err
	}

	// Always try root prefix first
	rootPrefix := fmt.Sprintf("owners/%s/snapshots/", userID)
	allKeys, err := s.listKeysForPrefix(ctx, rootPrefix)
	if err != nil {
		return nil, err
	}

	// If root prefix yielded no snapshots, check configured and discovered prefixes
	if len(allKeys) == 0 {
		seenKeys := make(map[string]bool)
		for _, k := range allKeys {
			seenKeys[k] = true
		}

		for _, prefix := range s.candidatePrefixes(ctx, userID) {
			if prefix == rootPrefix {
				continue
			}
			keys, err := s.listKeysForPrefix(ctx, prefix)
			if err != nil {
				return nil, err
			}
			for _, k := range keys {
				if !seenKeys[k] {
					seenKeys[k] = true
					allKeys = append(allKeys, k)
				}
			}
		}
	}

	manifests, err := s.fetchManifestsConcurrently(ctx, allKeys)
	if err != nil {
		return nil, err
	}

	seenSelectors := make(map[string]bool)
	var deduped []*model.SnapshotManifest
	for _, m := range manifests {
		if !seenSelectors[m.Selector] {
			seenSelectors[m.Selector] = true
			deduped = append(deduped, m)
		}
	}
	return deduped, nil
}

func (s *S3Store) listObjectsPage(ctx context.Context, prefix, token, delimiter string) (*listObjectsV2Output, error) {
	params := url.Values{}
	params.Set("list-type", "2")
	params.Set("prefix", prefix)
	if token != "" {
		params.Set("continuation-token", token)
	}
	if delimiter != "" {
		params.Set("delimiter", delimiter)
	}

	reqURL := s.buildBucketURL(params)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL.String(), nil)
	if err != nil {
		return nil, err
	}

	s.signRequest(req, nil, time.Now())

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return &listObjectsV2Output{}, nil
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("s3 list objects error (status %d): %s", resp.StatusCode, string(body))
	}

	var output listObjectsV2Output
	if err := xml.NewDecoder(resp.Body).Decode(&output); err != nil {
		return nil, fmt.Errorf("failed to decode list objects XML: %w", err)
	}

	return &output, nil
}

func (s *S3Store) fetchManifestsConcurrently(ctx context.Context, keys []string) ([]*model.SnapshotManifest, error) {
	if len(keys) == 0 {
		return []*model.SnapshotManifest{}, nil
	}

	type fetchResult struct {
		manifest *model.SnapshotManifest
		err      error
	}

	results := make([]fetchResult, len(keys))
	concurrency := 10
	if len(keys) < concurrency {
		concurrency = len(keys)
	}

	semaphore := make(chan struct{}, concurrency)
	var wg sync.WaitGroup

	for i, key := range keys {
		wg.Add(1)
		go func(idx int, objKey string) {
			defer wg.Done()
			select {
			case semaphore <- struct{}{}:
			case <-ctx.Done():
				results[idx] = fetchResult{err: ctx.Err()}
				return
			}
			defer func() { <-semaphore }()

			manifest, err := s.fetchObjectByKey(ctx, objKey)
			results[idx] = fetchResult{manifest: manifest, err: err}
		}(i, key)
	}

	wg.Wait()

	manifests := make([]*model.SnapshotManifest, 0, len(keys))
	for _, res := range results {
		if res.err != nil {
			return nil, res.err
		}
		if res.manifest != nil {
			manifests = append(manifests, res.manifest)
		}
	}

	return manifests, nil
}

// GetSnapshot retrieves a single snapshot manifest by its selector.
func (s *S3Store) GetSnapshot(ctx context.Context, userID, selector string) (*model.SnapshotManifest, error) {
	if err := validateUserID(userID); err != nil {
		return nil, err
	}
	if err := validateSelector(selector); err != nil {
		return nil, err
	}

	rootKey := fmt.Sprintf("owners/%s/snapshots/%s.json", userID, selector)
	manifest, err := s.fetchObjectByKey(ctx, rootKey)
	if err == nil {
		return manifest, nil
	} else if !errors.Is(err, ErrNotFound) {
		return nil, err
	}

	for _, prefix := range s.candidatePrefixes(ctx, userID) {
		if prefix == fmt.Sprintf("owners/%s/snapshots/", userID) {
			continue
		}
		key := fmt.Sprintf("%s%s.json", prefix, selector)
		m, err := s.fetchObjectByKey(ctx, key)
		if err == nil {
			return m, nil
		} else if !errors.Is(err, ErrNotFound) {
			return nil, err
		}
	}

	return nil, ErrNotFound
}

func (s *S3Store) fetchObjectByKey(ctx context.Context, key string) (*model.SnapshotManifest, error) {
	reqURL := s.buildObjectURL(key)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL.String(), nil)
	if err != nil {
		return nil, err
	}

	s.signRequest(req, nil, time.Now())

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrNotFound
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("s3 get object error (status %d): %s", resp.StatusCode, string(body))
	}

	var manifest model.SnapshotManifest
	if err := json.NewDecoder(resp.Body).Decode(&manifest); err != nil {
		return nil, fmt.Errorf("failed to decode snapshot manifest JSON: %w", err)
	}

	return &manifest, nil
}

func (s *S3Store) buildBucketURL(params url.Values) *url.URL {
	u := *s.baseURL
	u.Path = path.Join(u.Path, s.cfg.Bucket)
	if !strings.HasSuffix(u.Path, "/") {
		u.Path += "/"
	}
	u.RawQuery = params.Encode()
	return &u
}

func (s *S3Store) buildObjectURL(key string) *url.URL {
	u := *s.baseURL
	u.Path = path.Join(u.Path, s.cfg.Bucket, key)
	u.RawQuery = ""
	return &u
}

func validateUserID(userID string) error {
	if userID == "" || strings.Contains(userID, "/") || strings.Contains(userID, "\\") ||
		strings.Contains(userID, "..") || !validUserID.MatchString(userID) {
		return ErrInvalidUserID
	}
	return nil
}

func validateSelector(selector string) error {
	if selector == "" || strings.Contains(selector, "/") || strings.Contains(selector, "\\") ||
		strings.Contains(selector, "..") || !validSelector.MatchString(selector) {
		return ErrInvalidSelector
	}
	return nil
}

func (s *S3Store) signRequest(req *http.Request, body []byte, signTime time.Time) {
	if s.cfg.AccessKeyID == "" && s.cfg.SecretAccessKey == "" {
		return
	}

	amzDate := signTime.UTC().Format("20060102T150405Z")
	dateStamp := signTime.UTC().Format("20060102")

	payloadHash := hex.EncodeToString(sha256Hash(body))
	req.Header.Set("x-amz-date", amzDate)
	req.Header.Set("x-amz-content-sha256", payloadHash)
	if req.Host != "" {
		req.Header.Set("Host", req.Host)
	} else {
		req.Header.Set("Host", req.URL.Host)
	}

	if s.cfg.SessionToken != "" {
		req.Header.Set("x-amz-security-token", s.cfg.SessionToken)
	}

	headersToSign := []string{"host", "x-amz-content-sha256", "x-amz-date"}
	if s.cfg.SessionToken != "" {
		headersToSign = append(headersToSign, "x-amz-security-token")
	}
	sort.Strings(headersToSign)

	var canonicalHeaders strings.Builder
	for _, h := range headersToSign {
		canonicalHeaders.WriteString(h)
		canonicalHeaders.WriteString(":")
		canonicalHeaders.WriteString(strings.TrimSpace(req.Header.Get(h)))
		canonicalHeaders.WriteString("\n")
	}
	signedHeaders := strings.Join(headersToSign, ";")

	canonicalURI := req.URL.EscapedPath()
	if canonicalURI == "" {
		canonicalURI = "/"
	}

	canonicalQuery := buildCanonicalQueryString(req.URL.Query())

	canonicalRequest := strings.Join([]string{
		req.Method,
		canonicalURI,
		canonicalQuery,
		canonicalHeaders.String(),
		signedHeaders,
		payloadHash,
	}, "\n")

	credentialScope := fmt.Sprintf("%s/%s/s3/aws4_request", dateStamp, s.cfg.Region)
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256",
		amzDate,
		credentialScope,
		hex.EncodeToString(sha256Hash([]byte(canonicalRequest))),
	}, "\n")

	signingKey := getSigningKey(s.cfg.SecretAccessKey, dateStamp, s.cfg.Region, "s3")
	signature := hex.EncodeToString(hmacSHA256(signingKey, []byte(stringToSign)))

	authHeader := fmt.Sprintf(
		"AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		s.cfg.AccessKeyID,
		credentialScope,
		signedHeaders,
		signature,
	)
	req.Header.Set("Authorization", authHeader)
}

func buildCanonicalQueryString(values url.Values) string {
	if len(values) == 0 {
		return ""
	}
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var parts []string
	for _, k := range keys {
		vals := values[k]
		sort.Strings(vals)
		escapedK := rfc3986Escape(k)
		for _, v := range vals {
			parts = append(parts, escapedK+"="+rfc3986Escape(v))
		}
	}
	return strings.Join(parts, "&")
}

func rfc3986Escape(s string) string {
	return strings.ReplaceAll(url.QueryEscape(s), "+", "%20")
}

func sha256Hash(data []byte) []byte {
	hash := sha256.Sum256(data)
	return hash[:]
}

func hmacSHA256(key, data []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(data)
	return mac.Sum(nil)
}

func getSigningKey(secretKey, dateStamp, region, service string) []byte {
	kDate := hmacSHA256([]byte("AWS4"+secretKey), []byte(dateStamp))
	kRegion := hmacSHA256(kDate, []byte(region))
	kService := hmacSHA256(kRegion, []byte(service))
	return hmacSHA256(kService, []byte("aws4_request"))
}
