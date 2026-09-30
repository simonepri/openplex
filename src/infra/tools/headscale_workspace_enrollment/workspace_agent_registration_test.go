// Tests provisioner agent registration, asynchronous Coder build binding, and token verification.

package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

const (
	fixtureAgentToken = "423e4567-e89b-42d3-a456-426614174000"
	fixtureBuildID    = "523e4567-e89b-42d3-a456-426614174000"
)

type fixedAgentValidator struct {
	err   error
	token string
}

func (v *fixedAgentValidator) validate(_ context.Context, token string) error {
	v.token = token
	return v.err
}

type fixedProvisionerReviewer struct {
	err error
}

func (v fixedProvisionerReviewer) authenticate(_ context.Context, token, audience, username string) error {
	if v.err != nil {
		return v.err
	}
	if token != "provisioner-token" || audience != registrationAudience || username != registrationServiceAccount {
		return errors.New("unexpected provisioner identity")
	}
	return nil
}

type mutableWorkspaceReader struct {
	mu      sync.Mutex
	current coderWorkspace
}

func (r *mutableWorkspaceReader) workspace(_ context.Context, _, _ string) (coderWorkspace, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.current, nil
}

func (r *mutableWorkspaceReader) set(workspace coderWorkspace) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.current = workspace
}

func TestWorkspaceAgentRegistrationFinalizesAndAuthenticates(t *testing.T) {
	request := validWorkspaceAgentRegistration()
	reader := &mutableWorkspaceReader{current: registrationWorkspace(request, "running", "succeeded")}
	validator := &fixedAgentValidator{}
	registry := testWorkspaceAgentRegistry(t, reader, validator)
	session := []byte("owner-session")
	status, err := registry.begin(request, session)
	if err != nil || status != http.StatusAccepted {
		t.Fatalf("registration returned %d, %v", status, err)
	}
	waitForRegistration(t, registry, request.WorkspaceID)

	stored, err := os.ReadFile(registry.path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(stored), request.AgentToken) || strings.Contains(string(stored), "owner-session") {
		t.Fatal("registration persisted a bearer credential")
	}
	wantHash := "d2c174ef89840722a8149937f126df8ae05cabd3b8702eb3464eb1d067d04186"
	if workspaceAgentTokenHash(request.AgentToken) != wantHash || !strings.Contains(string(stored), wantHash) {
		t.Fatal("registration did not use the domain-separated token hash")
	}
	id, err := registry.authenticate(context.Background(), request.AgentToken)
	if err != nil || id.AgentID != fixtureAgentID || id.WorkspaceID != fixtureWorkspaceID ||
		id.SubjectID != fixtureUserID || id.Team != "examples" {
		t.Fatalf("registered identity = %#v, %v", id, err)
	}
	if validator.token != request.AgentToken {
		t.Fatal("runtime validation did not prove the current raw token to Coder")
	}
	if _, err := registry.authenticate(context.Background(), "623e4567-e89b-42d3-a456-426614174000"); err == nil {
		t.Fatal("unregistered agent token hash was accepted")
	}
	validator.err = errors.New("Coder rejected stopped agent")
	if _, err := registry.authenticate(context.Background(), request.AgentToken); err == nil {
		t.Fatal("Coder-rejected completed workspace token was accepted")
	}
	validator.err = nil
	stopping := registrationWorkspace(request, "stopping", "succeeded")
	stopping.LatestBuild.Transition = "stop"
	reader.set(stopping)
	if _, err := registry.authenticate(context.Background(), request.AgentToken); err != nil {
		t.Fatalf("still-connected agent was rejected during active stop: %v", err)
	}

	replaySession := []byte("replay-session")
	status, err = registry.begin(request, replaySession)
	if err != nil || status != http.StatusAccepted {
		t.Fatalf("completed registration replay returned %d, %v", status, err)
	}
	if string(replaySession) != strings.Repeat("\x00", len(replaySession)) {
		t.Fatal("completed replay retained its owner session")
	}
}

func TestWorkspaceAgentBindingCacheObservesAtomicReplacement(t *testing.T) {
	first := validWorkspaceAgentRegistration()
	registry := registeredLimiterRegistry(t, first)
	firstHash := workspaceAgentTokenHash(first.AgentToken)
	if _, err := registry.binding(firstHash); err != nil {
		t.Fatal(err)
	}
	second := first
	second.AgentToken = "623e4567-e89b-42d3-a456-426614174000"
	second.BuildID = "723e4567-e89b-42d3-a456-426614174000"
	second.WorkspaceID = "823e4567-e89b-42d3-a456-426614174000"
	secondHash := workspaceAgentTokenHash(second.AgentToken)
	if err := writeWorkspaceAgentBindings(registry.path, []workspaceAgentBinding{{
		AgentID: "a23e4567-e89b-42d3-a456-426614174000", TokenHash: secondHash,
		workspaceAgentMetadata: second.metadata(),
	}}); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.binding(firstHash); err == nil {
		t.Fatal("reader retained a binding replaced by the private broker")
	}
	if _, err := registry.binding(secondHash); err != nil {
		t.Fatalf("reader missed an atomically published binding: %v", err)
	}
}

func TestPendingRegistrationIsLostAcrossRestartAndClearsSession(t *testing.T) {
	request := validWorkspaceAgentRegistration()
	reader := &mutableWorkspaceReader{current: registrationWorkspace(request, "starting", "running")}
	registry := testWorkspaceAgentRegistry(t, reader, &fixedAgentValidator{})
	registry.finalizeTimeout = 20 * time.Millisecond
	registry.pollInterval = time.Millisecond
	session := []byte("owner-session")
	if _, err := registry.begin(request, session); err != nil {
		t.Fatal(err)
	}
	waitForRegistration(t, registry, request.WorkspaceID)
	if string(session) != strings.Repeat("\x00", len(session)) {
		t.Fatal("expired pending registration retained its owner session")
	}
	restarted := testWorkspaceAgentRegistryAt(registry.path, reader, &fixedAgentValidator{})
	if _, err := restarted.authenticate(context.Background(), request.AgentToken); err == nil {
		t.Fatal("broker restart promoted a pending registration")
	}
}

func TestRegistrationRejectsPrebuildAndNonSuccessfulBuilds(t *testing.T) {
	request := validWorkspaceAgentRegistration()
	for name, mutate := range map[string]func(*coderWorkspace){
		"prebuild": func(workspace *coderWorkspace) { workspace.IsPrebuild = true },
		"failed start": func(workspace *coderWorkspace) {
			workspace.LatestBuild.Status = "failed"
			workspace.LatestBuild.Job.Status = "failed"
		},
		"completed stop": func(workspace *coderWorkspace) {
			workspace.LatestBuild.Status = "stopped"
			workspace.LatestBuild.Job.Status = "succeeded"
			workspace.LatestBuild.Transition = "stop"
		},
		"newer start": func(workspace *coderWorkspace) {
			workspace.LatestBuild.ID = "623e4567-e89b-42d3-a456-426614174000"
		},
		"multiple main agents": func(workspace *coderWorkspace) {
			workspace.LatestBuild.Resources[0].Agents = append(
				workspace.LatestBuild.Resources[0].Agents,
				coderWorkspaceAgent{ID: "623e4567-e89b-42d3-a456-426614174000", Name: "main"},
			)
		},
	} {
		t.Run(name, func(t *testing.T) {
			workspace := registrationWorkspace(request, "running", "succeeded")
			mutate(&workspace)
			reader := &mutableWorkspaceReader{current: workspace}
			registry := testWorkspaceAgentRegistry(t, reader, &fixedAgentValidator{})
			registry.finalizeTimeout = 20 * time.Millisecond
			registry.pollInterval = time.Millisecond
			if _, err := registry.begin(request, []byte("owner-session")); err != nil {
				t.Fatal(err)
			}
			waitForRegistration(t, registry, request.WorkspaceID)
			if _, err := registry.binding(workspaceAgentTokenHash(request.AgentToken)); err == nil {
				t.Fatal("invalid Coder build produced a durable agent binding")
			}
		})
	}
	claim := true
	request.IsPrebuildClaim = &claim
	if validateWorkspaceAgentRegistration(request) == nil {
		t.Fatal("prebuild claim registration was accepted")
	}
}

func TestRegistrationRejectsTokenReuseAndFencesSupersededFinalizer(t *testing.T) {
	first := validWorkspaceAgentRegistration()
	reader := &mutableWorkspaceReader{current: registrationWorkspace(first, "starting", "running")}
	registry := testWorkspaceAgentRegistry(t, reader, &fixedAgentValidator{})
	registry.finalizeTimeout = time.Second
	if _, err := registry.begin(first, []byte("first-session")); err != nil {
		t.Fatal(err)
	}
	second := first
	second.WorkspaceID = "623e4567-e89b-42d3-a456-426614174000"
	second.BuildID = "723e4567-e89b-42d3-a456-426614174000"
	if _, err := registry.begin(second, []byte("second-session")); err == nil {
		t.Fatal("same agent token was accepted for another workspace/build")
	}

	registry.pendingMu.Lock()
	old := registry.pending[first.WorkspaceID]
	registry.pendingMu.Unlock()
	newerRequest := first
	newerRequest.AgentToken = "623e4567-e89b-42d3-a456-426614174000"
	newerRequest.BuildID = "723e4567-e89b-42d3-a456-426614174000"
	newer := &pendingWorkspaceAgent{metadata: newerRequest.metadata(), tokenHash: workspaceAgentTokenHash(newerRequest.AgentToken)}
	registry.pendingMu.Lock()
	registry.pending[first.WorkspaceID] = newer
	registry.pendingMu.Unlock()
	newBinding := workspaceAgentBinding{
		AgentID: fixtureAgentID, TokenHash: newer.tokenHash, workspaceAgentMetadata: newer.metadata,
	}
	if err := registry.storePending(newer, newBinding); err != nil {
		t.Fatal(err)
	}
	oldBinding := workspaceAgentBinding{
		AgentID: fixtureAgentID, TokenHash: old.tokenHash, workspaceAgentMetadata: old.metadata,
	}
	if err := registry.storePending(old, oldBinding); err == nil {
		t.Fatal("superseded finalizer overwrote the newer binding")
	}
	bindings, err := readWorkspaceAgentBindings(registry.path)
	if err != nil || len(bindings) != 1 || bindings[0].BuildID != newer.metadata.BuildID {
		t.Fatalf("newer binding was not retained: %#v, %v", bindings, err)
	}
	reused := newerRequest
	reused.WorkspaceID = "823e4567-e89b-42d3-a456-426614174000"
	reused.BuildID = "923e4567-e89b-42d3-a456-426614174000"
	if _, err := registry.begin(reused, []byte("reused-session")); err == nil {
		t.Fatal("durably bound agent token was reused for another build")
	}
	old.cancel()
}

func TestNewerBuildReplacesPendingRegistrationForWorkspace(t *testing.T) {
	first := validWorkspaceAgentRegistration()
	reader := &mutableWorkspaceReader{current: registrationWorkspace(first, "starting", "running")}
	registry := testWorkspaceAgentRegistry(t, reader, &fixedAgentValidator{})
	registry.finalizeTimeout = time.Second
	if _, err := registry.begin(first, []byte("first-session")); err != nil {
		t.Fatal(err)
	}

	second := first
	second.BuildID = "623e4567-e89b-42d3-a456-426614174000"
	reader.set(registrationWorkspace(second, "starting", "running"))
	if _, err := registry.begin(second, []byte("second-session")); err != nil {
		t.Fatalf("newer build with the workspace agent token was rejected: %v", err)
	}
	registry.pendingMu.Lock()
	current := registry.pending[first.WorkspaceID]
	registry.pendingMu.Unlock()
	if current == nil || current.metadata.BuildID != second.BuildID ||
		current.tokenHash != workspaceAgentTokenHash(second.AgentToken) {
		t.Fatal("newer workspace build did not replace the pending registration")
	}
	current.cancel()
}

func TestWorkspaceAgentRegistrationHandlerRequiresEveryIdentityBoundary(t *testing.T) {
	request := validWorkspaceAgentRegistration()
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	workspace := registrationWorkspace(request, "starting", "running")
	reader := &mutableWorkspaceReader{current: workspace}
	registry := testWorkspaceAgentRegistry(t, reader, &fixedAgentValidator{})
	registry.finalizeTimeout = 20 * time.Millisecond
	path := filepath.Join(t.TempDir(), "owners.json")
	if err := writeBindings(path, []binding{{
		Issuer: "https://dex.example", Subject: "subject", PreferredUsername: "ldap", OwnerID: fixtureUserID,
	}}); err != nil {
		t.Fatal(err)
	}
	validOwner := coderUser{
		ID: fixtureUserID, Email: "ldap@example.com", Groups: []string{"team:examples"},
		Issuer: "https://dex.example", Subject: "subject", PreferredUsername: "ldap",
	}
	for name, configure := range map[string]func(*server, *http.Request){
		"wrong service account": func(server *server, _ *http.Request) {
			server.provisionerAuth = fixedProvisionerReviewer{err: errors.New("wrong service account")}
		},
		"wrong owner": func(server *server, _ *http.Request) {
			owner := validOwner
			owner.ID = "623e4567-e89b-42d3-a456-426614174000"
			server.ownerAuth = fixedOwnerAuthenticator{owner: owner}
		},
		"wrong team": func(server *server, _ *http.Request) {
			owner := validOwner
			owner.Groups = []string{"team:foreign"}
			server.ownerAuth = fixedOwnerAuthenticator{owner: owner}
		},
		"missing provisioner token": func(_ *server, request *http.Request) {
			request.Header.Del("Authorization")
		},
	} {
		t.Run(name, func(t *testing.T) {
			s := server{
				agentRegistry: registry, bindingPath: path, bindingsMu: &sync.Mutex{},
				ownerAuth: fixedOwnerAuthenticator{owner: validOwner}, provisionerAuth: fixedProvisionerReviewer{},
			}
			httpRequest := httptest.NewRequest(http.MethodPost, registrationPath, strings.NewReader(string(body)))
			httpRequest.Header.Set("Authorization", "Bearer provisioner-token")
			httpRequest.Header.Set("Coder-Session-Token", "owner-session")
			configure(&s, httpRequest)
			response := httptest.NewRecorder()
			s.registerWorkspaceAgent(response, httpRequest)
			if response.Code == http.StatusAccepted {
				t.Fatal("registration crossed an invalid identity boundary")
			}
		})
	}
}

func TestWorkspaceAgentRegistrationHandlerReturnsEmptyAcceptedResponse(t *testing.T) {
	request := validWorkspaceAgentRegistration()
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	reader := &mutableWorkspaceReader{current: registrationWorkspace(request, "starting", "running")}
	registry := testWorkspaceAgentRegistry(t, reader, &fixedAgentValidator{})
	registry.finalizeTimeout = 20 * time.Millisecond
	path := filepath.Join(t.TempDir(), "owners.json")
	if err := writeBindings(path, []binding{{
		Issuer: "https://dex.example", Subject: "subject", PreferredUsername: "ldap", OwnerID: fixtureUserID,
	}}); err != nil {
		t.Fatal(err)
	}
	s := server{
		agentRegistry: registry, bindingPath: path, bindingsMu: &sync.Mutex{},
		ownerAuth: fixedOwnerAuthenticator{owner: coderUser{
			ID: fixtureUserID, Email: "ldap@example.com", Groups: []string{"team:examples"},
			Issuer: "https://dex.example", Subject: "subject", PreferredUsername: "ldap",
		}},
		provisionerAuth: fixedProvisionerReviewer{},
	}
	httpRequest := httptest.NewRequest(http.MethodPost, registrationPath, strings.NewReader(string(body)))
	httpRequest.Header.Set("Authorization", "Bearer provisioner-token")
	httpRequest.Header.Set("Coder-Session-Token", "owner-session")
	response := httptest.NewRecorder()
	s.registerWorkspaceAgent(response, httpRequest)
	if response.Code != http.StatusAccepted || response.Body.Len() != 0 {
		t.Fatalf("registration response = %d %q", response.Code, response.Body.String())
	}
	registry.pendingMu.Lock()
	registry.pending[request.WorkspaceID].cancel()
	registry.pendingMu.Unlock()
}

func TestKubernetesTokenReviewerRequiresExactAudienceAndServiceAccount(t *testing.T) {
	credentialPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(credentialPath, []byte("reviewer-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	responseUsername := registrationServiceAccount
	responseAudience := registrationAudience
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/apis/authentication.k8s.io/v1/tokenreviews" ||
			request.Header.Get("Authorization") != "Bearer reviewer-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		var review struct {
			Spec struct {
				Audiences []string `json:"audiences"`
				Token     string   `json:"token"`
			} `json:"spec"`
		}
		if err := json.NewDecoder(request.Body).Decode(&review); err != nil || review.Spec.Token != "provisioner-token" ||
			len(review.Spec.Audiences) != 1 || review.Spec.Audiences[0] != registrationAudience {
			http.Error(w, "invalid", http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"status": map[string]any{
			"authenticated": true, "audiences": []string{responseAudience},
			"user": map[string]string{"username": responseUsername},
		}})
	}))
	defer api.Close()
	reviewer := kubernetesTokenReviewer{client: api.Client(), credentialPath: credentialPath, endpoint: api.URL}
	if err := reviewer.authenticate(context.Background(), "provisioner-token", registrationAudience, registrationServiceAccount); err != nil {
		t.Fatalf("valid provisioner TokenReview failed: %v", err)
	}
	responseUsername = "system:serviceaccount:default:default"
	if err := reviewer.authenticate(context.Background(), "provisioner-token", registrationAudience, registrationServiceAccount); err == nil {
		t.Fatal("wrong provisioner service account was accepted")
	}
	responseUsername = registrationServiceAccount
	responseAudience = "https://kubernetes.default.svc"
	if err := reviewer.authenticate(context.Background(), "provisioner-token", registrationAudience, registrationServiceAccount); err == nil {
		t.Fatal("wrong TokenReview audience was accepted")
	}
}

func TestCoderWebsocketValidatorRequiresCurrentAgentToken(t *testing.T) {
	accepted := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v2/workspaceagents/me/rpc" ||
			request.URL.Query().Get("role") != "snapshot-broker" ||
			request.URL.Query().Get("version") != "2.10" ||
			request.Header.Get("Coder-Session-Token") != fixtureAgentToken {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		connection, err := websocket.Accept(w, request, nil)
		if err == nil {
			_ = connection.CloseNow()
		}
	}))
	defer accepted.Close()
	validator := coderWebsocketValidator{baseURL: accepted.URL, client: accepted.Client(), timeout: time.Second}
	if err := validator.validate(context.Background(), fixtureAgentToken); err != nil {
		t.Fatalf("current Coder agent token was rejected: %v", err)
	}

	rejected := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
	}))
	defer rejected.Close()
	validator = coderWebsocketValidator{baseURL: rejected.URL, client: rejected.Client(), timeout: time.Second}
	if err := validator.validate(context.Background(), fixtureAgentToken); err == nil {
		t.Fatal("Coder-rejected agent token was accepted")
	}

	stalled := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		time.Sleep(100 * time.Millisecond)
	}))
	defer stalled.Close()
	validator = coderWebsocketValidator{baseURL: stalled.URL, client: stalled.Client(), timeout: 10 * time.Millisecond}
	started := time.Now()
	if err := validator.validate(context.Background(), fixtureAgentToken); err == nil {
		t.Fatal("stalled Coder token proof was accepted")
	}
	if time.Since(started) > time.Second {
		t.Fatal("Coder token proof exceeded its timeout")
	}
}

func validWorkspaceAgentRegistration() workspaceAgentRegistrationRequest {
	prebuild := false
	return workspaceAgentRegistrationRequest{
		AgentToken: fixtureAgentToken, BuildID: fixtureBuildID, Cell: "cell-eaws-lh1",
		Incarnation: "eaws-lh1", IsPrebuildClaim: &prebuild,
		Lineage: "0123456789abcdef0123456789abcdef01234567", Machine: "ldap-dev",
		OwnerID: fixtureUserID, Team: "examples", WorkspaceID: fixtureWorkspaceID,
		WorkspaceName: "dev", WorkspaceVolume: "/var/lib/workspace",
	}
}

func registrationWorkspace(request workspaceAgentRegistrationRequest, status, jobStatus string) coderWorkspace {
	return coderWorkspace{
		ID: request.WorkspaceID, Name: request.WorkspaceName, OwnerID: request.OwnerID,
		LatestBuild: coderWorkspaceBuild{
			ID: request.BuildID, Job: coderProvisionerJob{Status: jobStatus}, Status: status,
			Transition: "start", WorkspaceID: request.WorkspaceID, WorkspaceOwnerID: request.OwnerID,
			Resources: []coderWorkspaceResource{{Agents: []coderWorkspaceAgent{{ID: fixtureAgentID, Name: "main"}}}},
		},
	}
}

func testWorkspaceAgentRegistry(
	t *testing.T,
	reader workspaceReader,
	validator agentTokenValidator,
) *workspaceAgentRegistry {
	t.Helper()
	return testWorkspaceAgentRegistryAt(filepath.Join(t.TempDir(), "agents.json"), reader, validator)
}

func testWorkspaceAgentRegistryAt(
	path string,
	reader workspaceReader,
	validator agentTokenValidator,
) *workspaceAgentRegistry {
	return &workspaceAgentRegistry{
		finalizeTimeout: time.Second, mu: &sync.Mutex{}, path: path,
		pending: map[string]*pendingWorkspaceAgent{}, pendingMu: &sync.Mutex{},
		pollInterval: time.Millisecond, reader: reader, validator: validator,
	}
}

func waitForRegistration(t *testing.T, registry *workspaceAgentRegistry, workspaceID string) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		registry.pendingMu.Lock()
		pending := registry.pending[workspaceID]
		registry.pendingMu.Unlock()
		if pending == nil {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("workspace agent registration did not finish")
}

func TestWorkspaceAgentRegistrationAncestryValidation(t *testing.T) {
	// Canonical lineage format accepted
	req := validWorkspaceAgentRegistration()
	req.Lineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	if err := validateWorkspaceAgentRegistration(req); err != nil {
		t.Fatalf("canonical lineage was rejected: %v", err)
	}

	// Root registration with parent lineage must fail
	req = validWorkspaceAgentRegistration()
	isRoot := true
	req.IsRoot = &isRoot
	req.ParentLineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	if err := validateWorkspaceAgentRegistration(req); err == nil {
		t.Fatal("root registration with parent lineage was accepted")
	}

	// Non-root registration without parent lineage must fail
	req = validWorkspaceAgentRegistration()
	notRoot := false
	req.IsRoot = &notRoot
	req.ParentLineage = ""
	req.ParentSnapshot = "manifest-parent"
	if err := validateWorkspaceAgentRegistration(req); err == nil {
		t.Fatal("non-root registration without parent lineage was accepted")
	}

	// Non-root registration with valid parent lineage and snapshot must succeed
	req = validWorkspaceAgentRegistration()
	req.IsRoot = &notRoot
	req.ParentLineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	req.ParentSnapshot = "manifest-parent"
	if err := validateWorkspaceAgentRegistration(req); err != nil {
		t.Fatalf("valid non-root registration was rejected: %v", err)
	}

	// Nil IsRoot without parent lineage is treated as root and succeeds
	req = validWorkspaceAgentRegistration()
	req.IsRoot = nil
	req.ParentLineage = ""
	req.ParentSnapshot = ""
	if err := validateWorkspaceAgentRegistration(req); err != nil {
		t.Fatalf("nil IsRoot was rejected: %v", err)
	}

	// Nil IsRoot with parent lineage must fail
	req = validWorkspaceAgentRegistration()
	req.IsRoot = nil
	req.ParentLineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	if err := validateWorkspaceAgentRegistration(req); err == nil {
		t.Fatal("nil IsRoot with parent lineage was accepted")
	}

	// Direct validateWorkspaceAgentMetadata with nil IsRoot
	meta := req.metadata()
	meta.IsRoot = nil
	meta.ParentLineage = ""
	meta.ParentSnapshot = ""
	if err := validateWorkspaceAgentMetadata(meta); err != nil {
		t.Fatalf("metadata with nil IsRoot was rejected: %v", err)
	}

	meta.ParentLineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	if err := validateWorkspaceAgentMetadata(meta); err == nil {
		t.Fatal("metadata with nil IsRoot and parent lineage was accepted")
	}
}

func TestLegacyDurableBindingWithoutIsRootDecodesAndAuthenticates(t *testing.T) {
	legacyJSON := `{
		"schema": 1,
		"bindings": [
			{
				"agentId": "` + fixtureAgentID + `",
				"tokenHash": "` + workspaceAgentTokenHash(fixtureAgentToken) + `",
				"buildId": "` + fixtureBuildID + `",
				"cell": "cell-eaws-lh1",
				"incarnation": "eaws-lh1",
				"lineage": "0123456789abcdef0123456789abcdef01234567",
				"machine": "ldap-dev",
				"ownerId": "` + fixtureUserID + `",
				"team": "examples",
				"workspaceId": "` + fixtureWorkspaceID + `",
				"workspaceName": "dev",
				"workspaceVolume": "/var/lib/workspace"
			}
		]
	}`
	path := filepath.Join(t.TempDir(), "agents.json")
	if err := os.WriteFile(path, []byte(legacyJSON), 0600); err != nil {
		t.Fatal(err)
	}

	bindings, err := readWorkspaceAgentBindings(path)
	if err != nil {
		t.Fatalf("legacy bindings failed to decode: %v", err)
	}
	if len(bindings) != 1 {
		t.Fatalf("expected 1 binding, got %d", len(bindings))
	}
	if bindings[0].IsRoot != nil {
		t.Fatalf("expected IsRoot to be nil in decoded legacy binding, got %v", *bindings[0].IsRoot)
	}
	if err := validateWorkspaceAgentMetadata(bindings[0].workspaceAgentMetadata); err != nil {
		t.Fatalf("legacy binding metadata failed validation: %v", err)
	}

	registry := testWorkspaceAgentRegistryAt(path, &mutableWorkspaceReader{}, &fixedAgentValidator{})
	id, err := registry.authenticate(context.Background(), fixtureAgentToken)
	if err != nil {
		t.Fatalf("failed to authenticate legacy binding: %v", err)
	}
	if !id.IsRoot {
		t.Fatal("expected legacy binding without isRoot to authenticate as root")
	}
}

