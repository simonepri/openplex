// Reconciles Headscale pre-auth keys into Kubernetes Secret records for local Tailscale routing.

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"syscall"
	"time"
)

// LINT.IfChange(router-auth-key-contract)
const (
	authTagsArgument    = "tag:k8s-egress,tag:subnet-router"
	defaultRetry        = 30 * time.Second
	defaultReconcile    = 24 * time.Hour
	rotationLead        = 24 * time.Hour
	keyIDAnnotation     = "headscale-preauth-key-id"
	expiryAnnotation    = "headscale-preauth-expiration-epoch"
	keyDigestAnnotation = "headscale-preauth-key-sha256"
	headscaleBinary     = "/ko-app/headscale"
	headscaleConfig     = "/etc/headscale/config.yaml"
	keyExpiration       = "720h"
	readyPath           = "/tmp/ready"
	serviceAccountCA    = "/var/run/secrets/kubernetes-api/ca.crt"
	serviceAccountToken = "/var/run/secrets/kubernetes-api/token"
)

var requiredAuthTags = []string{"tag:k8s-egress", "tag:subnet-router"}

// LINT.ThenChange(//src/infra/tools/cloud_emulator/auth/headscale_keys.py:router-auth-key-contract)

var (
	dnsLabel     = regexp.MustCompile(`^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$`)
	headscaleKey = regexp.MustCompile(`^hskey-auth-([A-Za-z0-9_-]{12})-[A-Za-z0-9_-]{64}$`)
)

type handoff struct {
	Version  int           `json:"version"`
	Provider string        `json:"provider"`
	Source   handoffSource `json:"source"`
	Target   handoffTarget `json:"target"`
}

type handoffSource struct {
	Namespace       string `json:"namespace"`
	RecordName      string `json:"recordName"`
	SecretStoreName string `json:"secretStoreName"`
}

type handoffTarget struct {
	Namespace  string `json:"namespace"`
	SecretName string `json:"secretName"`
}

type secret struct {
	Metadata secretMetadata    `json:"metadata"`
	Data     map[string]string `json:"data"`
}

type secretMetadata struct {
	Annotations map[string]string `json:"annotations"`
}

type preAuthKey struct {
	ID         uint64       `json:"id"`
	Key        string       `json:"key"`
	Reusable   bool         `json:"reusable"`
	Expiration protobufTime `json:"expiration"`
	ACLTags    []string     `json:"acl_tags"`
}

type protobufTime struct {
	Seconds int64 `json:"seconds"`
	Nanos   int32 `json:"nanos"`
}

type kubernetesClient struct {
	baseURL    string
	httpClient *http.Client
	tokenPath  string
}

type headscaleClient interface {
	list(context.Context) ([]preAuthKey, error)
	create(context.Context) (preAuthKey, error)
}

type commandHeadscaleClient struct {
	binary string
	config string
}

type producer struct {
	handoff    handoff
	kubernetes *kubernetesClient
	headscale  headscaleClient
	now        func() time.Time
	pending    *preAuthKey
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "ready" {
		if _, err := os.Stat(readyPath); err != nil {
			os.Exit(1)
		}
		return
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	configuredHandoff, err := parseHandoff(os.Getenv("HEADSCALE_PREAUTH_HANDOFF"))
	if err != nil {
		logger.Error("invalid handoff", "error", err)
		os.Exit(1)
	}
	kubernetes, err := newKubernetesClient()
	if err != nil {
		logger.Error("invalid Kubernetes client configuration", "error", err)
		os.Exit(1)
	}
	reconciler := &producer{
		handoff:    configuredHandoff,
		kubernetes: kubernetes,
		headscale:  commandHeadscaleClient{binary: headscaleBinary, config: headscaleConfig},
		now:        time.Now,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	run(ctx, reconciler, logger, defaultReconcile, defaultRetry)
}

func run(
	ctx context.Context,
	reconciler *producer,
	logger *slog.Logger,
	reconcileInterval time.Duration,
	retryInterval time.Duration,
) {
	for {
		rotated, err := reconcileAndReady(ctx, reconciler, markReady)
		if err == nil {
			if rotated {
				logger.Info("published Headscale pre-auth record")
			}
		} else if !errors.Is(err, context.Canceled) {
			logger.Error("Headscale pre-auth reconciliation failed", "error", err)
		}

		delay := reconcileInterval
		if err != nil {
			delay = retryInterval
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

func reconcileAndReady(
	ctx context.Context,
	reconciler *producer,
	ready func() error,
) (bool, error) {
	rotated, err := reconciler.reconcile(ctx)
	if err != nil {
		return false, err
	}
	if err := ready(); err != nil {
		return false, fmt.Errorf("publish readiness: %w", err)
	}
	return rotated, nil
}

func (p *producer) reconcile(ctx context.Context) (bool, error) {
	if p.pending != nil {
		if err := p.publish(ctx, *p.pending); err != nil {
			return false, err
		}
		p.pending = nil
		return true, nil
	}

	record, err := p.kubernetes.secret(ctx, p.handoff.Source.Namespace, p.handoff.Source.RecordName)
	if err != nil {
		return false, fmt.Errorf("read source record: %w", err)
	}
	keys, err := p.headscale.list(ctx)
	if err != nil {
		return false, fmt.Errorf("list Headscale pre-auth keys: %w", err)
	}
	if recordCurrent(record, keys, p.now().Add(rotationLead)) {
		return false, nil
	}

	created, err := p.headscale.create(ctx)
	if err != nil {
		return false, fmt.Errorf("create Headscale pre-auth key: %w", err)
	}
	if err := validateCreatedKey(created, p.now().Add(rotationLead)); err != nil {
		return false, err
	}
	p.pending = &created
	if err := p.publish(ctx, created); err != nil {
		return false, err
	}
	p.pending = nil
	return true, nil
}

func (p *producer) publish(ctx context.Context, key preAuthKey) error {
	patch := map[string]any{
		"metadata": map[string]any{
			"annotations": map[string]string{
				keyIDAnnotation:     strconv.FormatUint(key.ID, 10),
				expiryAnnotation:    strconv.FormatInt(key.Expiration.Seconds, 10),
				keyDigestAnnotation: authKeyDigest([]byte(key.Key)),
			},
		},
		"data": map[string]string{
			"authkey": base64.StdEncoding.EncodeToString([]byte(key.Key)),
		},
	}
	if err := p.kubernetes.patchSecret(
		ctx,
		p.handoff.Source.Namespace,
		p.handoff.Source.RecordName,
		patch,
	); err != nil {
		return fmt.Errorf("publish source record: %w", err)
	}
	return nil
}

func parseHandoff(raw string) (handoff, error) {
	var parsed handoff
	decoder := json.NewDecoder(bytes.NewBufferString(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&parsed); err != nil {
		return handoff{}, fmt.Errorf("decode JSON-v1 handoff: %w", err)
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return handoff{}, errors.New("handoff must contain one JSON object")
	}
	if parsed.Version != 1 {
		return handoff{}, errors.New("handoff.version must be 1")
	}
	if parsed.Provider != "floci" {
		return handoff{}, errors.New("handoff.provider must be floci")
	}
	if parsed.Source.Namespace != "secret-records" ||
		parsed.Source.SecretStoreName != "local-secret-records" {
		return handoff{}, errors.New("handoff source must use the local Kubernetes secret-record store")
	}
	if len(parsed.Source.RecordName) > 63 || !dnsLabel.MatchString(parsed.Source.RecordName) {
		return handoff{}, errors.New("handoff.source.recordName must be a DNS label")
	}
	if parsed.Target.Namespace != "tailscale-system" || parsed.Target.SecretName != "headscale-preauth" {
		return handoff{}, errors.New("handoff target must be tailscale-system/headscale-preauth")
	}
	return parsed, nil
}

func verifyRecordAuthKey(record secret) ([]byte, bool) {
	encodedKey := record.Data["authkey"]
	if encodedKey == "" {
		return nil, false
	}
	decoded, err := base64.StdEncoding.DecodeString(encodedKey)
	if err != nil || len(decoded) == 0 {
		return nil, false
	}
	storedDigest, err := hex.DecodeString(record.Metadata.Annotations[keyDigestAnnotation])
	if err != nil || len(storedDigest) != sha256.Size {
		return nil, false
	}
	computedDigest := sha256.Sum256(decoded)
	if subtle.ConstantTimeCompare(computedDigest[:], storedDigest) != 1 {
		return nil, false
	}
	return maskedHeadscaleKey(decoded)
}

func parseRecordAnnotations(record secret, cutoff time.Time) (uint64, int64, bool) {
	keyID, err := strconv.ParseUint(record.Metadata.Annotations[keyIDAnnotation], 10, 64)
	if err != nil || keyID <= 0 {
		return 0, 0, false
	}
	expiry, err := strconv.ParseInt(record.Metadata.Annotations[expiryAnnotation], 10, 64)
	if err != nil || time.Unix(expiry, 0).Before(cutoff) {
		return 0, 0, false
	}
	return keyID, expiry, true
}

func matchAuthKey(key preAuthKey, keyID uint64, expiry int64, maskedKey []byte) bool {
	return key.ID == keyID &&
		subtle.ConstantTimeCompare(maskedKey, []byte(key.Key)) == 1 &&
		key.Reusable &&
		key.Expiration.Seconds == expiry &&
		hasRequiredAuthTags(key.ACLTags)
}

func recordCurrent(record secret, keys []preAuthKey, cutoff time.Time) bool {
	maskedKey, ok := verifyRecordAuthKey(record)
	if !ok {
		return false
	}
	keyID, expiry, ok := parseRecordAnnotations(record, cutoff)
	if !ok {
		return false
	}
	for _, key := range keys {
		if matchAuthKey(key, keyID, expiry, maskedKey) {
			return true
		}
	}
	return false
}

func validateCreatedKey(key preAuthKey, cutoff time.Time) error {
	_, keyValid := maskedHeadscaleKey([]byte(key.Key))
	if key.ID <= 0 || !keyValid || !key.Reusable ||
		!hasRequiredAuthTags(key.ACLTags) ||
		time.Unix(key.Expiration.Seconds, int64(key.Expiration.Nanos)).Before(cutoff) {
		return errors.New("Headscale returned an invalid pre-auth key record")
	}
	return nil
}

func hasRequiredAuthTags(tags []string) bool {
	if len(tags) != len(requiredAuthTags) {
		return false
	}
	for _, required := range requiredAuthTags {
		if !slices.Contains(tags, required) {
			return false
		}
	}
	return true
}

func authKeyDigest(key []byte) string {
	digest := sha256.Sum256(key)
	return hex.EncodeToString(digest[:])
}

func maskedHeadscaleKey(key []byte) ([]byte, bool) {
	parts := headscaleKey.FindSubmatch(key)
	if len(parts) != 2 {
		return nil, false
	}
	return []byte("hskey-auth-" + string(parts[1]) + "-***"), true
}

func (c commandHeadscaleClient) list(ctx context.Context) ([]preAuthKey, error) {
	output, err := c.execute(ctx, "preauthkeys", "list", "--output", "json")
	if err != nil {
		return nil, err
	}
	var keys []preAuthKey
	if err := json.Unmarshal(output, &keys); err != nil {
		return nil, errors.New("Headscale returned an invalid pre-auth key list")
	}
	return keys, nil
}

func (c commandHeadscaleClient) create(ctx context.Context) (preAuthKey, error) {
	output, err := c.execute(
		ctx,
		"preauthkeys",
		"create",
		"--reusable",
		"--expiration",
		keyExpiration,
		"--tags",
		authTagsArgument,
		"--output",
		"json",
	)
	if err != nil {
		return preAuthKey{}, err
	}
	var key preAuthKey
	if err := json.Unmarshal(output, &key); err != nil {
		return preAuthKey{}, errors.New("Headscale returned an invalid created-key record")
	}
	return key, nil
}

func (c commandHeadscaleClient) execute(ctx context.Context, args ...string) ([]byte, error) {
	commandContext, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	commandArgs := append([]string{"--config", c.config}, args...)
	command := exec.CommandContext(commandContext, c.binary, commandArgs...) // #nosec G204 -- binary and arguments are constants.
	var stdout bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = io.Discard
	if err := command.Run(); err != nil {
		return nil, fmt.Errorf("headscale command failed: %w", err)
	}
	return stdout.Bytes(), nil
}

func newKubernetesClient() (*kubernetesClient, error) {
	host := os.Getenv("KUBERNETES_SERVICE_HOST")
	port := os.Getenv("KUBERNETES_SERVICE_PORT_HTTPS")
	if host == "" || port == "" {
		return nil, errors.New("Kubernetes service address is missing")
	}
	caPEM, err := os.ReadFile(serviceAccountCA)
	if err != nil {
		return nil, fmt.Errorf("read Kubernetes CA: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("Kubernetes CA contains no certificate")
	}
	return &kubernetesClient{
		baseURL: "https://" + host + ":" + port,
		httpClient: &http.Client{
			Timeout: 20 * time.Second,
			Transport: &http.Transport{TLSClientConfig: &tls.Config{
				MinVersion: tls.VersionTLS12,
				RootCAs:    roots,
			}},
		},
		tokenPath: serviceAccountToken,
	}, nil
}

func (c *kubernetesClient) secret(ctx context.Context, namespace string, name string) (secret, error) {
	var result secret
	if err := c.request(ctx, http.MethodGet, secretURL(c.baseURL, namespace, name), nil, &result); err != nil {
		return secret{}, err
	}
	return result, nil
}

func (c *kubernetesClient) patchSecret(
	ctx context.Context,
	namespace string,
	name string,
	patch map[string]any,
) error {
	return c.request(ctx, http.MethodPatch, secretURL(c.baseURL, namespace, name), patch, nil)
}

func (c *kubernetesClient) request(
	ctx context.Context,
	method string,
	endpoint string,
	body any,
	result any,
) error {
	var encoded []byte
	var err error
	if body != nil {
		encoded, err = json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode Kubernetes request: %w", err)
		}
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return fmt.Errorf("create Kubernetes request: %w", err)
	}
	token, err := os.ReadFile(c.tokenPath)
	if err != nil {
		return fmt.Errorf("read projected Kubernetes token: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+string(bytes.TrimSpace(token)))
	request.Header.Set("Accept", "application/json")
	if body != nil {
		request.Header.Set("Content-Type", "application/merge-patch+json")
	}
	response, err := c.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("call Kubernetes API: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, response.Body)
		return fmt.Errorf("Kubernetes API returned %s", response.Status)
	}
	if result == nil {
		_, err = io.Copy(io.Discard, response.Body)
		return err
	}
	if err := json.NewDecoder(response.Body).Decode(result); err != nil {
		return fmt.Errorf("decode Kubernetes response: %w", err)
	}
	return nil
}

func secretURL(baseURL string, namespace string, name string) string {
	return fmt.Sprintf(
		"%s/api/v1/namespaces/%s/secrets/%s",
		baseURL,
		url.PathEscape(namespace),
		url.PathEscape(name),
	)
}

func markReady() error {
	if err := os.MkdirAll(filepath.Dir(readyPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(readyPath, nil, 0o600)
}
