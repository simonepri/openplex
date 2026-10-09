// Resolves virtual S3 URIs to direct physical storage coordinates for AWS and GCP.
//
// TODO(simonepri): Extract and publish multi-cloud S3 gateway resolver as a standalone open-source proxy.

package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

// CellBucket defines bucket names for a specific storage class.
type CellBucket struct {
	Account string `json:"account"`
	Name    string `json:"name"`
}

// CellConfig defines topology parameters for one cluster cell.
type CellConfig struct {
	Name               string                `json:"name"`
	Provider           string                `json:"provider"`
	Region             string                `json:"region"`
	VirtualName        string                `json:"virtualName"`
	Endpoint           string                `json:"endpoint,omitempty"`
	CrossRegionServer  string                `json:"crossRegionServer,omitempty"`
	CrossRegionService string                `json:"crossRegionService,omitempty"`
	Buckets            map[string]CellBucket `json:"buckets"`
}

// GlobalConfig defines global storage topology.
type GlobalConfig struct {
	Endpoint     string `json:"endpoint,omitempty"`
	BucketPrefix string `json:"bucketPrefix,omitempty"`
	BucketSuffix string `json:"bucketSuffix,omitempty"`
}

// TopologyConfig defines the entire cluster storage topology.
type TopologyConfig struct {
	TargetCell string       `json:"targetCell"`
	Cells      []CellConfig `json:"cells"`
	Global     GlobalConfig `json:"global,omitempty"`
}

// AuthInfo describes how the client authenticates to the resolved target.
type AuthInfo struct {
	Type string `json:"type"`
}

// ResolutionResponse represents the resolved S3 coordinates.
type ResolutionResponse struct {
	Mode        string   `json:"mode"`
	URI         string   `json:"uri"`
	Bucket      string   `json:"bucket"`
	Key         string   `json:"key"`
	EndpointURL string   `json:"endpoint_url"`
	Region      string   `json:"region"`
	Auth        AuthInfo `json:"auth"`
}

// ErrorResponse represents an HTTP error body.
type ErrorResponse struct {
	Error string `json:"error"`
}

// CallerIdentity holds authenticated caller context.
type CallerIdentity struct {
	Team           string
	Class          string // "team" or "system_mover"
	Namespace      string
	ServiceAccount string
}

// TokenReviewInfo contains the cryptographically verified identity from Kubernetes TokenReview.
type TokenReviewInfo struct {
	Authenticated  bool
	Username       string
	Namespace      string
	ServiceAccount string
	Audiences      []string
	Error          string
}

// TokenReviewer authenticates tokens against Kubernetes authentication.k8s.io/v1 TokenReview API.
type TokenReviewer interface {
	ReviewToken(ctx context.Context, token string) (*TokenReviewInfo, error)
}

type kubernetesTokenReviewer struct {
	client         *http.Client
	endpoint       string
	credentialPath string
	audience       string
}

type tokenReviewRequestBody struct {
	APIVersion string                 `json:"apiVersion"`
	Kind       string                 `json:"kind"`
	Spec       tokenReviewRequestSpec `json:"spec"`
}

type tokenReviewRequestSpec struct {
	Token     string   `json:"token"`
	Audiences []string `json:"audiences,omitempty"`
}

type tokenReviewResponseBody struct {
	APIVersion string                    `json:"apiVersion"`
	Kind       string                    `json:"kind"`
	Status     tokenReviewResponseStatus `json:"status"`
}

type tokenReviewResponseStatus struct {
	Authenticated bool                    `json:"authenticated"`
	Audiences     []string                `json:"audiences,omitempty"`
	Error         string                  `json:"error,omitempty"`
	User          tokenReviewResponseUser `json:"user,omitempty"`
}

type tokenReviewResponseUser struct {
	Username string              `json:"username"`
	UID      string              `json:"uid,omitempty"`
	Groups   []string            `json:"groups,omitempty"`
	Extra    map[string][]string `json:"extra,omitempty"`
}

func (k *kubernetesTokenReviewer) ReviewToken(ctx context.Context, token string) (*TokenReviewInfo, error) {
	if token == "" {
		return nil, errors.New("empty bearer token")
	}

	reqBody := tokenReviewRequestBody{
		APIVersion: "authentication.k8s.io/v1",
		Kind:       "TokenReview",
		Spec: tokenReviewRequestSpec{
			Token: token,
		},
	}
	if k.audience != "" {
		reqBody.Spec.Audiences = []string{k.audience}
	}

	bodyBytes, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal TokenReview request: %w", err)
	}

	apiEndpoint := strings.TrimRight(k.endpoint, "/") + "/apis/authentication.k8s.io/v1/tokenreviews"
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, apiEndpoint, bytes.NewReader(bodyBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create TokenReview request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	if k.credentialPath != "" {
		credential, err := os.ReadFile(k.credentialPath)
		if err == nil && len(credential) > 0 {
			httpReq.Header.Set("Authorization", "Bearer "+string(bytes.TrimSpace(credential)))
		}
	}

	client := k.client
	if client == nil {
		client = http.DefaultClient
	}

	resp, err := client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("TokenReview request to %s failed: %w", apiEndpoint, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("TokenReview API returned HTTP %s", resp.Status)
	}

	var review tokenReviewResponseBody
	if err := json.NewDecoder(resp.Body).Decode(&review); err != nil {
		return nil, fmt.Errorf("failed to decode TokenReview response: %w", err)
	}

	if !review.Status.Authenticated {
		return &TokenReviewInfo{
			Authenticated: false,
			Error:         review.Status.Error,
		}, nil
	}

	ns, sa, err := parseServiceAccountUsername(review.Status.User.Username)
	if err != nil {
		return nil, fmt.Errorf("failed to extract service account from TokenReview user: %w", err)
	}

	return &TokenReviewInfo{
		Authenticated:  true,
		Username:       review.Status.User.Username,
		Namespace:      ns,
		ServiceAccount: sa,
		Audiences:      review.Status.Audiences,
	}, nil
}

func parseServiceAccountUsername(username string) (string, string, error) {
	if !strings.HasPrefix(username, "system:serviceaccount:") {
		return "", "", fmt.Errorf("username %q is not a Kubernetes service account", username)
	}
	parts := strings.Split(username, ":")
	if len(parts) != 4 {
		return "", "", fmt.Errorf("invalid service account username structure %q", username)
	}
	namespace := parts[2]
	sa := parts[3]
	if namespace == "" || sa == "" {
		return "", "", fmt.Errorf("empty namespace or service account in username %q", username)
	}
	return namespace, sa, nil
}

var defaultTokenReviewer TokenReviewer

func newDefaultTokenReviewer() (TokenReviewer, error) {
	endpoint := os.Getenv("KUBERNETES_API_URL")
	if endpoint == "" {
		host := os.Getenv("KUBERNETES_SERVICE_HOST")
		port := os.Getenv("KUBERNETES_SERVICE_PORT")
		if host != "" && port != "" {
			endpoint = fmt.Sprintf("https://%s:%s", host, port)
		} else {
			endpoint = "https://kubernetes.default.svc"
		}
	}

	credentialPath := os.Getenv("KUBERNETES_TOKEN_PATH")
	if credentialPath == "" {
		credentialPath = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	}

	caPath := os.Getenv("KUBERNETES_CA_PATH")
	if caPath == "" {
		caPath = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	}

	audience := os.Getenv("KUBERNETES_TOKEN_AUDIENCE")
	if audience == "" {
		audience = "s3-resolver"
	}

	client, err := newKubernetesClient(caPath)
	if err != nil {
		return nil, err
	}

	return &kubernetesTokenReviewer{
		client:         client,
		endpoint:       endpoint,
		credentialPath: credentialPath,
		audience:       audience,
	}, nil
}

func newKubernetesClient(caPath string) (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if caPath != "" {
		if caData, err := os.ReadFile(caPath); err == nil && len(caData) > 0 {
			pool := x509.NewCertPool()
			if pool.AppendCertsFromPEM(caData) {
				transport.TLSClientConfig = &tls.Config{
					MinVersion: tls.VersionTLS12,
					RootCAs:    pool,
				}
			}
		}
	}
	return &http.Client{
		Transport: transport,
		Timeout:   10 * time.Second,
	}, nil
}

func bearerToken(value string) string {
	token, found := strings.CutPrefix(value, "Bearer ")
	if !found || token == "" || strings.TrimSpace(token) != token || strings.ContainsAny(token, " \t\r\n") {
		return ""
	}
	return token
}

func loadTopology(path string) (*TopologyConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read topology file %s: %w", path, err)
	}
	var cfg TopologyConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("failed to unmarshal topology JSON: %w", err)
	}
	return &cfg, nil
}

// resolveCallerIdentity authenticates caller identity strictly via Kubernetes TokenReview API.
// Caller-supplied bypass headers (e.g. X-Caller-Namespace, X-Trusted-Gateway) and ALLOW_TEST_HEADER are removed and rejected.
func resolveCallerIdentity(r *http.Request) (*CallerIdentity, error) {
	reviewer := defaultTokenReviewer
	if reviewer == nil {
		var err error
		reviewer, err = newDefaultTokenReviewer()
		if err != nil {
			return nil, fmt.Errorf("token reviewer unavailable: %w", err)
		}
	}
	return resolveCallerIdentityWithReviewer(r, reviewer)
}

func resolveCallerIdentityWithReviewer(r *http.Request, reviewer TokenReviewer) (*CallerIdentity, error) {
	// Reject caller-supplied identity or gateway bypass headers.
	if r.Header.Get("X-Caller-Namespace") != "" {
		return nil, errors.New("untrusted X-Caller-Namespace header")
	}
	if r.Header.Get("X-Trusted-Gateway") != "" {
		return nil, errors.New("untrusted X-Trusted-Gateway header")
	}

	authHeader := r.Header.Get("Authorization")
	token := bearerToken(authHeader)
	if token == "" {
		return nil, errors.New("missing or invalid caller credentials")
	}

	parts := strings.Split(token, ".")
	if len(parts) == 3 && parts[2] == "" {
		return nil, errors.New("unsigned bearer token")
	}

	if reviewer == nil {
		return nil, errors.New("token reviewer unavailable")
	}

	info, err := reviewer.ReviewToken(r.Context(), token)
	if err != nil {
		return nil, fmt.Errorf("token review failed: %w", err)
	}
	if !info.Authenticated {
		if info.Error != "" {
			return nil, fmt.Errorf("token authentication failed: %s", info.Error)
		}
		return nil, errors.New("token authentication failed: token not authenticated")
	}

	targetAudience := os.Getenv("KUBERNETES_TOKEN_AUDIENCE")
	if targetAudience == "" {
		targetAudience = "s3-resolver"
	}
	if len(info.Audiences) > 0 {
		hasAudience := false
		for _, aud := range info.Audiences {
			if aud == targetAudience {
				hasAudience = true
				break
			}
		}
		if !hasAudience {
			return nil, fmt.Errorf("token audience mismatch: token audiences %v do not contain %q", info.Audiences, targetAudience)
		}
	}

	if info.Namespace == "" || info.ServiceAccount == "" {
		if info.Username != "" {
			var err error
			info.Namespace, info.ServiceAccount, err = parseServiceAccountUsername(info.Username)
			if err != nil {
				return nil, fmt.Errorf("invalid caller identity: %w", err)
			}
		} else {
			return nil, errors.New("missing caller namespace or service account in token review")
		}
	}

	return identityFromServiceAccount(info.Namespace, info.ServiceAccount)
}

func identityFromServiceAccount(ns, sa string) (*CallerIdentity, error) {
	ident, err := identityFromNamespace(ns)
	if err != nil {
		return nil, err
	}
	ident.ServiceAccount = sa
	return ident, nil
}

func identityFromNamespace(ns string) (*CallerIdentity, error) {
	if strings.HasPrefix(ns, "team-") {
		slug := strings.TrimPrefix(ns, "team-")
		if idx := strings.Index(slug, "-"); idx != -1 {
			slug = slug[:idx]
		}
		return &CallerIdentity{
			Team:           slug,
			Class:          "team",
			Namespace:      ns,
			ServiceAccount: "default",
		}, nil
	}

	// System namespaces recognized as system movers
	switch ns {
	case "s3-system", "kube-system", "velero-system", "coder-workspace-backup-system":
		return &CallerIdentity{
			Team:           "",
			Class:          "system_mover",
			Namespace:      ns,
			ServiceAccount: "default",
		}, nil
	default:
		return nil, fmt.Errorf("unauthorized caller namespace %q: namespace must be an active team or managed platform mover", ns)
	}
}

// ParsedURI decomposes a virtual S3 URI.
type ParsedURI struct {
	VirtualCell string
	Class       string
	Team        string
	Path        string
	IsGlobal    bool
}

func parseVirtualURI(rawURI string) (*ParsedURI, error) {
	if !strings.HasPrefix(rawURI, "s3://") {
		return nil, fmt.Errorf("URI must start with s3://: %s", rawURI)
	}

	trimmed := strings.TrimPrefix(rawURI, "s3://")
	if trimmed == "global/meta" {
		return &ParsedURI{
			VirtualCell: "global",
			Class:       "meta",
			Team:        "",
			Path:        "",
			IsGlobal:    true,
		}, nil
	}
	if strings.HasPrefix(trimmed, "global/meta/") {
		path := strings.TrimPrefix(trimmed, "global/meta/")
		return &ParsedURI{
			VirtualCell: "global",
			Class:       "meta",
			Team:        "",
			Path:        path,
			IsGlobal:    true,
		}, nil
	}

	parts := strings.SplitN(trimmed, "/", 4)
	if len(parts) < 3 {
		return nil, fmt.Errorf("invalid virtual S3 URI format, expected s3://<cell>/<class>/<team>/<path>: %s", rawURI)
	}

	cellName := parts[0]
	className := parts[1]
	teamName := parts[2]
	var restPath string
	if len(parts) == 4 {
		restPath = parts[3]
	}

	if cellName == "global" {
		return &ParsedURI{
			VirtualCell: cellName,
			Class:       className,
			Team:        teamName,
			Path:        restPath,
			IsGlobal:    true,
		}, nil
	}

	return &ParsedURI{
		VirtualCell: cellName,
		Class:       className,
		Team:        teamName,
		Path:        restPath,
		IsGlobal:    false,
	}, nil
}

func findCell(cfg *TopologyConfig, nameOrVirtual string) *CellConfig {
	for _, cell := range cfg.Cells {
		if cell.VirtualName == nameOrVirtual || cell.Name == nameOrVirtual {
			return &cell
		}
	}
	return nil
}

func resolveGlobalURI(cfg *TopologyConfig, caller *CallerIdentity, parsed *ParsedURI) (*ResolutionResponse, int, error) {
	if cfg.Global.Endpoint == "" || cfg.Global.BucketPrefix == "" || cfg.Global.BucketSuffix == "" {
		return nil, http.StatusInternalServerError, errors.New("global storage is not configured")
	}

	var team string
	if parsed.Class == "meta" {
		if caller == nil || caller.Class != "team" || caller.Team == "" {
			return nil, http.StatusForbidden, errors.New("only team callers may access global meta storage")
		}
		team = caller.Team
	} else {
		team = parsed.Team
		if team == "" {
			return nil, http.StatusBadRequest, errors.New("cannot determine team for global URI")
		}
		if caller != nil && caller.Class == "team" && caller.Team != "" && caller.Team != parsed.Team {
			return nil, http.StatusForbidden, fmt.Errorf("caller from team %q cannot access prefix for team %q", caller.Team, parsed.Team)
		}
	}

	key := parsed.Class
	if parsed.Path != "" {
		key = fmt.Sprintf("%s/%s", parsed.Class, parsed.Path)
	}

	bucket := fmt.Sprintf("%s-%s-%s", cfg.Global.BucketPrefix, team, cfg.Global.BucketSuffix)

	return &ResolutionResponse{
		Mode:        "direct",
		URI:         fmt.Sprintf("s3://%s/%s", bucket, key),
		Bucket:      bucket,
		Key:         key,
		EndpointURL: cfg.Global.Endpoint,
		Region:      "auto",
		Auth: AuthInfo{
			Type: "ambient_workload_identity",
		},
	}, http.StatusOK, nil
}

func resolveCellURI(caller *CallerIdentity, localCell, targetCell *CellConfig, parsed *ParsedURI) (*ResolutionResponse, int, error) {
	key := fmt.Sprintf("%s/%s/%s", parsed.Class, parsed.Team, parsed.Path)
	if parsed.Path == "" {
		key = fmt.Sprintf("%s/%s", parsed.Class, parsed.Team)
	}

	if targetCell.Name == localCell.Name {
		bucketCfg, ok := targetCell.Buckets[parsed.Class]
		if !ok {
			return nil, http.StatusBadRequest, fmt.Errorf("storage class %q not declared in cell %s", parsed.Class, targetCell.Name)
		}
		return &ResolutionResponse{
			Mode:        "direct",
			URI:         fmt.Sprintf("s3://%s/%s", bucketCfg.Name, key),
			Bucket:      bucketCfg.Name,
			Key:         key,
			EndpointURL: getEndpointURL(targetCell),
			Region:      targetCell.Region,
			Auth: AuthInfo{
				Type: "ambient_workload_identity",
			},
		}, http.StatusOK, nil
	}

	if caller.Class == "team" {
		endpointURL := "http://s3-gateway.s3-system.svc"
		return &ResolutionResponse{
			Mode:        "metered_proxy",
			URI:         fmt.Sprintf("s3://%s/%s", parsed.VirtualCell, key),
			Bucket:      parsed.VirtualCell,
			Key:         key,
			EndpointURL: endpointURL,
			Region:      localCell.Region,
			Auth: AuthInfo{
				Type: "gateway_credential",
			},
		}, http.StatusOK, nil
	}

	bucketCfg, ok := targetCell.Buckets[parsed.Class]
	if !ok {
		return nil, http.StatusBadRequest, fmt.Errorf("storage class %q not declared in remote cell %s", parsed.Class, targetCell.Name)
	}

	return &ResolutionResponse{
		Mode:        "direct_federated",
		URI:         fmt.Sprintf("s3://%s/%s", bucketCfg.Name, key),
		Bucket:      bucketCfg.Name,
		Key:         key,
		EndpointURL: getEndpointURL(targetCell),
		Region:      targetCell.Region,
		Auth: AuthInfo{
			Type: "federated_workload_identity",
		},
	}, http.StatusOK, nil
}

func resolveURI(cfg *TopologyConfig, caller *CallerIdentity, parsed *ParsedURI) (*ResolutionResponse, int, error) {
	if parsed.IsGlobal {
		return resolveGlobalURI(cfg, caller, parsed)
	}

	localCell := findCell(cfg, cfg.TargetCell)
	if localCell == nil {
		return nil, http.StatusInternalServerError, fmt.Errorf("local target cell %q not found in topology", cfg.TargetCell)
	}

	if caller != nil && caller.Class == "team" && caller.Team != "" && caller.Team != parsed.Team {
		return nil, http.StatusForbidden, fmt.Errorf("caller from team %q cannot access prefix for team %q", caller.Team, parsed.Team)
	}

	targetCell := findCell(cfg, parsed.VirtualCell)
	if targetCell == nil {
		return nil, http.StatusNotFound, fmt.Errorf("unknown storage cell %q", parsed.VirtualCell)
	}

	return resolveCellURI(caller, localCell, targetCell, parsed)
}

func getEndpointURL(cell *CellConfig) string {
	if cell.Endpoint != "" {
		return cell.Endpoint
	}
	switch cell.Provider {
	case "floci":
		return "http://localhost:4566"
	case "gcp":
		return "https://storage.googleapis.com"
	case "aws":
		return fmt.Sprintf("https://s3.%s.amazonaws.com", cell.Region)
	default:
		return fmt.Sprintf("https://s3.%s.amazonaws.com", cell.Region)
	}
}

func handleResolve(cfg *TopologyConfig, w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		_ = json.NewEncoder(w).Encode(ErrorResponse{Error: "method not allowed, use GET"})
		return
	}

	caller, err := resolveCallerIdentity(r)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_ = json.NewEncoder(w).Encode(ErrorResponse{Error: err.Error()})
		return
	}

	rawURI := r.URL.Query().Get("uri")
	if rawURI == "" {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(ErrorResponse{Error: "missing 'uri' query parameter"})
		return
	}

	parsed, err := parseVirtualURI(rawURI)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(ErrorResponse{Error: err.Error()})
		return
	}

	res, status, err := resolveURI(cfg, caller, parsed)
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(ErrorResponse{Error: err.Error()})
		return
	}

	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(res)
}

func handleHealthz(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func main() {
	configPath := os.Getenv("TOPOLOGY_CONFIG")
	if configPath == "" {
		configPath = "/etc/s3-resolver/topology.json"
	}

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	cfg, err := loadTopology(configPath)
	if err != nil {
		log.Fatalf("Fatal error loading topology: %v", err)
	}

	if defaultTokenReviewer == nil {
		reviewer, err := newDefaultTokenReviewer()
		if err != nil {
			log.Printf("Warning: failed to initialize Kubernetes token reviewer: %v", err)
		} else {
			defaultTokenReviewer = reviewer
		}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handleHealthz)
	mux.HandleFunc("/resolve", func(w http.ResponseWriter, r *http.Request) {
		handleResolve(cfg, w, r)
	})

	log.Printf("S3 Resolver listening on :%s (targetCell: %s)", port, cfg.TargetCell)
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Server exited with error: %v", err)
	}
}
