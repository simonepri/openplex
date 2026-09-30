// Tests S3 URI resolution, AWS and GCP routing logic, and HTTP endpoint handlers.

package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func mockTopology() *TopologyConfig {
	return &TopologyConfig{
		TargetCell: "cell-eaws-lh1",
		Cells: []CellConfig{
			{
				Name:        "cell-eaws-lh1",
				Provider:    "floci",
				Region:      "lh1",
				VirtualName: "eaws-lh1",
				Buckets: map[string]CellBucket{
					"home":    {Account: "", Name: "cloud-cell-eaws-lh1-home"},
					"meta":    {Account: "", Name: "cloud-cell-eaws-lh1-meta"},
					"scratch": {Account: "", Name: "cloud-cell-eaws-lh1-scratch"},
					"backups": {Account: "", Name: "cloud-cell-eaws-lh1-backups"},
					"archive": {Account: "", Name: "cloud-cell-eaws-lh1-archive"},
				},
			},
			{
				Name:        "cell-aws-us1",
				Provider:    "aws",
				Region:      "us-east-1",
				VirtualName: "aws-us1",
				Endpoint:    "https://s3.us-east-1.amazonaws.com",
				Buckets: map[string]CellBucket{
					"home":    {Account: "111122223333", Name: "prod-us1-home"},
					"scratch": {Account: "111122223333", Name: "prod-us1-scratch"},
				},
			},
			{
				Name:        "cell-gcp-usw1",
				Provider:    "gcp",
				Region:      "us-west1",
				VirtualName: "gcp-usw1",
				Buckets: map[string]CellBucket{
					"home":    {Account: "prod-gcp-project", Name: "prod-gcp-usw1-home"},
					"scratch": {Account: "prod-gcp-project", Name: "prod-gcp-usw1-scratch"},
				},
			},
		},
		Global: GlobalConfig{
			WriterCell:   "cell-eaws-lh1",
			ReplicaCells: []string{"cell-eaws-lh1", "cell-aws-us1", "cell-gcp-usw1"},
		},
	}
}

func TestParseVirtualURI(t *testing.T) {
	tests := []struct {
		name        string
		uri         string
		expectError bool
		expected    *ParsedURI
	}{
		{
			name: "valid team cell uri",
			uri:  "s3://eaws-lh1/scratch/team-a/checkpoints/epoch-1.pt",
			expected: &ParsedURI{
				VirtualCell: "eaws-lh1",
				Class:       "scratch",
				Team:        "team-a",
				Path:        "checkpoints/epoch-1.pt",
				IsGlobal:    false,
			},
		},
		{
			name: "valid team cell uri without subpath",
			uri:  "s3://eaws-lh1/scratch/team-a",
			expected: &ParsedURI{
				VirtualCell: "eaws-lh1",
				Class:       "scratch",
				Team:        "team-a",
				Path:        "",
				IsGlobal:    false,
			},
		},
		{
			name: "valid global uri",
			uri:  "s3://global/home/team-a/models/v1.bin",
			expected: &ParsedURI{
				VirtualCell: "global",
				Class:       "home",
				Team:        "team-a",
				Path:        "models/v1.bin",
				IsGlobal:    true,
			},
		},
		{
			name:        "invalid scheme",
			uri:         "https://s3.amazonaws.com/bucket/key",
			expectError: true,
		},
		{
			name:        "too few path segments",
			uri:         "s3://eaws-lh1/scratch",
			expectError: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			res, err := parseVirtualURI(tc.uri)
			if tc.expectError {
				if err == nil {
					t.Fatalf("expected error for %s, got nil", tc.uri)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if *res != *tc.expected {
				t.Errorf("got %+v, want %+v", *res, *tc.expected)
			}
		})
	}
}

type mockTokenReviewer struct {
	reviews map[string]*TokenReviewInfo
	err     error
}

func (m *mockTokenReviewer) ReviewToken(ctx context.Context, token string) (*TokenReviewInfo, error) {
	if m.err != nil {
		return nil, m.err
	}
	if info, ok := m.reviews[token]; ok {
		return info, nil
	}
	return &TokenReviewInfo{
		Authenticated: false,
		Error:         "token invalid or expired",
	}, nil
}

func makeJWT(namespace string) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	payload := base64.RawURLEncoding.EncodeToString([]byte(`{"kubernetes.io/namespace":"` + namespace + `","sub":"system:serviceaccount:` + namespace + `:default"}`))
	return header + "." + payload + ".signature"
}

func TestResolveCallerIdentity(t *testing.T) {
	jwtBeta := makeJWT("team-beta")
	jwtVelero := makeJWT("velero-system")

	reviewer := &mockTokenReviewer{
		reviews: map[string]*TokenReviewInfo{
			jwtBeta: {
				Authenticated:  true,
				Username:       "system:serviceaccount:team-beta:default",
				Namespace:      "team-beta",
				ServiceAccount: "default",
			},
			jwtVelero: {
				Authenticated:  true,
				Username:       "system:serviceaccount:velero-system:default",
				Namespace:      "velero-system",
				ServiceAccount: "default",
			},
			"token-user": {
				Authenticated: true,
				Username:      "system:node:worker-1",
			},
			"jwt-wrong-aud": {
				Authenticated:  true,
				Username:       "system:serviceaccount:team-beta:default",
				Namespace:      "team-beta",
				ServiceAccount: "default",
				Audiences:      []string{"wrong-audience"},
			},
		},
	}

	origReviewer := defaultTokenReviewer
	defaultTokenReviewer = reviewer
	defer func() { defaultTokenReviewer = origReviewer }()

	tests := []struct {
		name        string
		setupReq    func(r *http.Request)
		expectError bool
		expected    *CallerIdentity
	}{
		{
			name: "spoofed caller namespace with trusted gateway rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("X-Caller-Namespace", "team-alpha")
				r.Header.Set("X-Trusted-Gateway", "true")
			},
			expectError: true,
		},
		{
			name: "spoofed caller namespace system mover with trusted gateway rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("X-Caller-Namespace", "coder-workspace-backup-system")
				r.Header.Set("X-Trusted-Gateway", "true")
			},
			expectError: true,
		},
		{
			name: "spoofed caller namespace rejected without trusted gateway",
			setupReq: func(r *http.Request) {
				r.Header.Set("X-Caller-Namespace", "team-alpha")
			},
			expectError: true,
		},
		{
			name: "spoofed trusted gateway header only rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("X-Trusted-Gateway", "true")
			},
			expectError: true,
		},
		{
			name: "bearer jwt team authenticated via TokenReview",
			setupReq: func(r *http.Request) {
				r.Header.Set("Authorization", "Bearer "+jwtBeta)
			},
			expected: &CallerIdentity{
				Team:           "beta",
				Class:          "team",
				Namespace:      "team-beta",
				ServiceAccount: "default",
			},
		},
		{
			name: "bearer jwt system mover authenticated via TokenReview",
			setupReq: func(r *http.Request) {
				r.Header.Set("Authorization", "Bearer "+jwtVelero)
			},
			expected: &CallerIdentity{
				Team:           "",
				Class:          "system_mover",
				Namespace:      "velero-system",
				ServiceAccount: "default",
			},
		},
		{
			name: "bearer jwt audience mismatch rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("Authorization", "Bearer jwt-wrong-aud")
			},
			expectError: true,
		},
		{
			name: "bearer token invalid unauthenticated rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("Authorization", "Bearer invalid-token")
			},
			expectError: true,
		},
		{
			name: "bearer jwt unsigned rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("Authorization", "Bearer header.payload.")
			},
			expectError: true,
		},
		{
			name:        "missing credentials rejected",
			setupReq:    func(r *http.Request) {},
			expectError: true,
		},
		{
			name: "bearer token non serviceaccount rejected",
			setupReq: func(r *http.Request) {
				r.Header.Set("Authorization", "Bearer token-user")
			},
			expectError: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/resolve", nil)
			tc.setupReq(req)
			ident, err := resolveCallerIdentity(req)
			if tc.expectError {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if *ident != *tc.expected {
				t.Errorf("got %+v, want %+v", *ident, *tc.expected)
			}
		})
	}
}

func TestKubernetesTokenReviewer(t *testing.T) {
	credentialPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(credentialPath, []byte("reviewer-secret-token\n"), 0600); err != nil {
		t.Fatal(err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/apis/authentication.k8s.io/v1/tokenreviews" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer reviewer-secret-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Header.Get("Content-Type") != "application/json" {
			http.Error(w, "invalid content type", http.StatusBadRequest)
			return
		}

		var req tokenReviewRequestBody
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "invalid json", http.StatusBadRequest)
			return
		}

		if req.Spec.Token == "valid-sa-token" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "authentication.k8s.io/v1",
				"kind":       "TokenReview",
				"status": map[string]any{
					"authenticated": true,
					"user": map[string]any{
						"username": "system:serviceaccount:team-alpha:my-sa",
						"uid":      "sa-uid-1234",
					},
					"audiences": []string{"https://kubernetes.default.svc"},
				},
			})
			return
		}

		if req.Spec.Token == "invalid-token" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "authentication.k8s.io/v1",
				"kind":       "TokenReview",
				"status": map[string]any{
					"authenticated": false,
					"error":         "token expired or invalid",
				},
			})
			return
		}

		if req.Spec.Token == "user-token" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "authentication.k8s.io/v1",
				"kind":       "TokenReview",
				"status": map[string]any{
					"authenticated": true,
					"user": map[string]any{
						"username": "jane.doe@example.com",
					},
				},
			})
			return
		}

		http.Error(w, "unknown token", http.StatusBadRequest)
	}))
	defer server.Close()

	reviewer := &kubernetesTokenReviewer{
		client:         server.Client(),
		endpoint:       server.URL,
		credentialPath: credentialPath,
	}

	// 1. Valid token review
	info, err := reviewer.ReviewToken(context.Background(), "valid-sa-token")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !info.Authenticated {
		t.Fatal("expected token to be authenticated")
	}
	if info.Namespace != "team-alpha" {
		t.Errorf("got namespace %q, want team-alpha", info.Namespace)
	}
	if info.ServiceAccount != "my-sa" {
		t.Errorf("got service account %q, want my-sa", info.ServiceAccount)
	}

	// 2. Invalid token review
	info, err = reviewer.ReviewToken(context.Background(), "invalid-token")
	if err != nil {
		t.Fatalf("unexpected review error: %v", err)
	}
	if info.Authenticated {
		t.Fatal("expected token to be unauthenticated")
	}
	if info.Error != "token expired or invalid" {
		t.Errorf("got error %q, want token expired or invalid", info.Error)
	}

	// 3. User token (non-serviceaccount)
	_, err = reviewer.ReviewToken(context.Background(), "user-token")
	if err == nil {
		t.Fatal("expected error for non-serviceaccount user, got nil")
	}

	// 4. Empty token
	_, err = reviewer.ReviewToken(context.Background(), "")
	if err == nil {
		t.Fatal("expected error for empty token, got nil")
	}
}

func TestResolveURI(t *testing.T) {
	cfg := mockTopology()

	tests := []struct {
		name           string
		caller         *CallerIdentity
		uri            string
		expectedStatus int
		expectedMode   string
		expectedURI    string
		expectedHost   string
		expectedAuth   string
	}{
		{
			name: "in-region team direct (0 hops)",
			caller: &CallerIdentity{
				Team:  "examples",
				Class: "team",
			},
			uri:            "s3://eaws-lh1/scratch/examples/run-01/logs.txt",
			expectedStatus: http.StatusOK,
			expectedMode:   "direct",
			expectedURI:    "s3://cloud-cell-eaws-lh1-scratch/scratch/examples/run-01/logs.txt",
			expectedHost:   "http://localhost:4566",
			expectedAuth:   "ambient_workload_identity",
		},
		{
			name: "cross-region team metered proxy (1 hop)",
			caller: &CallerIdentity{
				Team:  "examples",
				Class: "team",
			},
			uri:            "s3://aws-us1/scratch/examples/data.parquet",
			expectedStatus: http.StatusOK,
			expectedMode:   "metered_proxy",
			expectedURI:    "s3://aws-us1/scratch/examples/data.parquet",
			expectedHost:   "http://s3-gateway.s3-system.svc",
			expectedAuth:   "gateway_credential",
		},
		{
			name: "cross-region system mover direct (0 hops)",
			caller: &CallerIdentity{
				Team:  "",
				Class: "system_mover",
			},
			uri:            "s3://aws-us1/home/examples/checkpoint.tar",
			expectedStatus: http.StatusOK,
			expectedMode:   "direct_federated",
			expectedURI:    "s3://prod-us1-home/home/examples/checkpoint.tar",
			expectedHost:   "https://s3.us-east-1.amazonaws.com",
			expectedAuth:   "federated_workload_identity",
		},
		{
			name: "cross-region gcp team metered proxy (1 hop)",
			caller: &CallerIdentity{
				Team:  "examples",
				Class: "team",
			},
			uri:            "s3://gcp-usw1/scratch/examples/data.parquet",
			expectedStatus: http.StatusOK,
			expectedMode:   "metered_proxy",
			expectedURI:    "s3://gcp-usw1/scratch/examples/data.parquet",
			expectedHost:   "http://s3-gateway.s3-system.svc",
			expectedAuth:   "gateway_credential",
		},
		{
			name: "cross-region gcp system mover direct (0 hops)",
			caller: &CallerIdentity{
				Team:  "",
				Class: "system_mover",
			},
			uri:            "s3://gcp-usw1/home/examples/model.bin",
			expectedStatus: http.StatusOK,
			expectedMode:   "direct_federated",
			expectedURI:    "s3://prod-gcp-usw1-home/home/examples/model.bin",
			expectedHost:   "https://storage.googleapis.com",
			expectedAuth:   "federated_workload_identity",
		},
		{
			name: "team isolation denial - foreign team prefix (403)",
			caller: &CallerIdentity{
				Team:  "team-a",
				Class: "team",
			},
			uri:            "s3://eaws-lh1/scratch/team-b/secret.key",
			expectedStatus: http.StatusForbidden,
		},
		{
			name: "global namespace direct resolution",
			caller: &CallerIdentity{
				Team:  "examples",
				Class: "team",
			},
			uri:            "s3://global/home/examples/dataset/train.csv",
			expectedStatus: http.StatusOK,
			expectedMode:   "direct",
			expectedURI:    "s3://cloud-cell-eaws-lh1-home/home/examples/dataset/train.csv",
			expectedHost:   "http://localhost:4566",
			expectedAuth:   "ambient_workload_identity",
		},
		{
			name: "unknown cell 404",
			caller: &CallerIdentity{
				Team:  "examples",
				Class: "team",
			},
			uri:            "s3://cell-unknown/scratch/examples/data",
			expectedStatus: http.StatusNotFound,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			parsed, err := parseVirtualURI(tc.uri)
			if err != nil {
				t.Fatalf("failed to parse URI: %v", err)
			}
			res, status, err := resolveURI(cfg, tc.caller, parsed)
			if status != tc.expectedStatus {
				t.Errorf("status = %d, want %d (err: %v)", status, tc.expectedStatus, err)
			}
			if tc.expectedStatus == http.StatusOK {
				if res == nil {
					t.Fatal("response is nil")
				}
				if res.Mode != tc.expectedMode {
					t.Errorf("mode = %q, want %q", res.Mode, tc.expectedMode)
				}
				if res.URI != tc.expectedURI {
					t.Errorf("uri = %q, want %q", res.URI, tc.expectedURI)
				}
				if res.EndpointURL != tc.expectedHost {
					t.Errorf("endpoint = %q, want %q", res.EndpointURL, tc.expectedHost)
				}
				if res.Auth.Type != tc.expectedAuth {
					t.Errorf("auth.type = %q, want %q", res.Auth.Type, tc.expectedAuth)
				}
			}
		})
	}
}

func TestHandleResolveHTTP(t *testing.T) {
	cfg := mockTopology()

	jwtExamples := makeJWT("team-examples")
	reviewer := &mockTokenReviewer{
		reviews: map[string]*TokenReviewInfo{
			jwtExamples: {
				Authenticated:  true,
				Username:       "system:serviceaccount:team-examples:default",
				Namespace:      "team-examples",
				ServiceAccount: "default",
			},
		},
	}
	origReviewer := defaultTokenReviewer
	defaultTokenReviewer = reviewer
	defer func() { defaultTokenReviewer = origReviewer }()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handleHealthz)
	mux.HandleFunc("/resolve", func(w http.ResponseWriter, r *http.Request) {
		handleResolve(cfg, w, r)
	})

	// 1. Health check
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz code = %d, want 200", rec.Code)
	}

	// 2. Successful resolve
	req = httptest.NewRequest(http.MethodGet, "/resolve?uri=s3://eaws-lh1/scratch/examples/file.parquet", nil)
	req.Header.Set("Authorization", "Bearer "+jwtExamples)
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("resolve code = %d, want 200, body: %s", rec.Code, rec.Body.String())
	}
	var res ResolutionResponse
	if err := json.NewDecoder(rec.Body).Decode(&res); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}
	if res.Mode != "direct" {
		t.Errorf("res.Mode = %q, want direct", res.Mode)
	}
	if !strings.Contains(res.URI, "cloud-cell-eaws-lh1-scratch") {
		t.Errorf("expected physical bucket in URI: %s", res.URI)
	}

	// 3. Unauthorized when spoofed headers are supplied
	req = httptest.NewRequest(http.MethodGet, "/resolve?uri=s3://eaws-lh1/scratch/examples/file.parquet", nil)
	req.Header.Set("X-Caller-Namespace", "team-examples")
	req.Header.Set("X-Trusted-Gateway", "true")
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("spoofed header code = %d, want 401", rec.Code)
	}

	// 4. Unauthorized when no auth
	req = httptest.NewRequest(http.MethodGet, "/resolve?uri=s3://eaws-lh1/scratch/examples/file.parquet", nil)
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized code = %d, want 401", rec.Code)
	}

	// 5. Unauthorized with invalid bearer token
	req = httptest.NewRequest(http.MethodGet, "/resolve?uri=s3://eaws-lh1/scratch/examples/file.parquet", nil)
	req.Header.Set("Authorization", "Bearer invalid-token")
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("invalid token code = %d, want 401", rec.Code)
	}

	// 6. Forbidden on cross-team prefix
	req = httptest.NewRequest(http.MethodGet, "/resolve?uri=s3://eaws-lh1/scratch/other-team/file.parquet", nil)
	req.Header.Set("Authorization", "Bearer "+jwtExamples)
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("forbidden code = %d, want 403", rec.Code)
	}
}

func TestResolveURIGCPLocal(t *testing.T) {
	cfg := mockTopology()
	cfg.TargetCell = "cell-gcp-usw1"

	caller := &CallerIdentity{
		Team:  "examples",
		Class: "team",
	}

	parsed, err := parseVirtualURI("s3://gcp-usw1/home/examples/data.parquet")
	if err != nil {
		t.Fatalf("unexpected parse error: %v", err)
	}

	res, status, err := resolveURI(cfg, caller, parsed)
	if status != http.StatusOK {
		t.Fatalf("status = %d, want 200 (err: %v)", status, err)
	}
	if res.Mode != "direct" {
		t.Errorf("mode = %q, want direct", res.Mode)
	}
	if res.EndpointURL != "https://storage.googleapis.com" {
		t.Errorf("endpoint = %q, want https://storage.googleapis.com", res.EndpointURL)
	}
	if res.Bucket != "prod-gcp-usw1-home" {
		t.Errorf("bucket = %q, want prod-gcp-usw1-home", res.Bucket)
	}
}

func TestGetEndpointURL(t *testing.T) {
	tests := []struct {
		name     string
		cell     *CellConfig
		expected string
	}{
		{
			name: "floci provider default",
			cell: &CellConfig{
				Provider: "floci",
				Region:   "lh1",
			},
			expected: "http://localhost:4566",
		},
		{
			name: "explicit endpoint overrides provider default",
			cell: &CellConfig{
				Provider: "floci",
				Endpoint: "http://custom-endpoint:4566",
			},
			expected: "http://custom-endpoint:4566",
		},
		{
			name: "gcp provider default",
			cell: &CellConfig{
				Provider: "gcp",
				Region:   "us-west1",
			},
			expected: "https://storage.googleapis.com",
		},
		{
			name: "aws provider default",
			cell: &CellConfig{
				Provider: "aws",
				Region:   "us-west-2",
			},
			expected: "https://s3.us-west-2.amazonaws.com",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := getEndpointURL(tc.cell); got != tc.expected {
				t.Errorf("getEndpointURL() = %s, want %s", got, tc.expected)
			}
		})
	}
}

func TestIdentityFromNamespace(t *testing.T) {
	tests := []struct {
		namespace     string
		expectedClass string
		expectedTeam  string
		expectError   bool
	}{
		{"s3-system", "system_mover", "", false},
		{"kube-system", "system_mover", "", false},
		{"velero-system", "system_mover", "", false},
		{"coder-workspace-backup-system", "system_mover", "", false},
		{"team-a", "team", "a", false},
		{"team-examples", "team", "examples", false},
		{"team-examples-ray-data", "team", "examples", false},
		{"team-examples-workspaces", "team", "examples", false},
		{"workspace-backup-system", "", "", true},
		{"velero", "", "", true},
		{"other", "", "", true},
		{"default", "", "", true},
	}
	for _, tc := range tests {
		t.Run(tc.namespace, func(t *testing.T) {
			ident, err := identityFromNamespace(tc.namespace)
			if tc.expectError {
				if err == nil {
					t.Fatalf("expected error for namespace %q, got nil", tc.namespace)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error for namespace %q: %v", tc.namespace, err)
			}
			if ident.Class != tc.expectedClass {
				t.Errorf("identityFromNamespace(%q).Class = %q, want %q", tc.namespace, ident.Class, tc.expectedClass)
			}
			if ident.Team != tc.expectedTeam {
				t.Errorf("identityFromNamespace(%q).Team = %q, want %q", tc.namespace, ident.Team, tc.expectedTeam)
			}
		})
	}
}

