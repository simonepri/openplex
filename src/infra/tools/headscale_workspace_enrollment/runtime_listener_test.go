// Tests Kubernetes API admission boundaries, agent isolation, and reverse-proxy ingress security.

package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

type blockingIssueCoordinator struct {
	entered chan struct{}
	release chan struct{}
}

func (c *blockingIssueCoordinator) issue(context.Context, identity, request) (string, error) {
	c.entered <- struct{}{}
	<-c.release
	return "single-use", nil
}

func (c *blockingIssueCoordinator) register(context.Context, identity, request, netip.Addr) error {
	return nil
}

func (c *blockingIssueCoordinator) revoke(context.Context, identity, request) error { return nil }

func (c *blockingIssueCoordinator) activeNodes(context.Context) ([]headscaleNode, error) {
	return nil, nil
}

func TestPrivateBrokerRoutesAreAbsentFromPublicListener(t *testing.T) {
	paths := []string{
		"/v1/bind",
		"/v1/resolve",
		registrationPath,
		"/v1/snapshots/repository",
		"/v1/snapshots/sign",
		"/v1/lineages/acquire",
		"/v1/lineages/renew",
		"/v1/lineages/release",
	}
	for _, path := range paths {
		t.Run(path, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{}`))
			response := httptest.NewRecorder()
			publicMuxWithUpstreams(server{}, http.NotFoundHandler(), http.NotFoundHandler()).ServeHTTP(response, request)
			if response.Code != http.StatusNotFound {
				t.Fatalf("private route returned %d from public listener", response.Code)
			}
		})
	}
}

func TestWorkspaceAPILimiterIsolatesRegisteredTokensFromFloods(t *testing.T) {
	first := validWorkspaceAgentRegistration()
	second := first
	second.AgentToken = "623e4567-e89b-42d3-a456-426614174000"
	second.BuildID = "723e4567-e89b-42d3-a456-426614174000"
	second.WorkspaceID = "823e4567-e89b-42d3-a456-426614174000"
	registry := registeredLimiterRegistry(t, first, second)
	limiter := newWorkspaceAPILimiter(registry)
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	limiter.now = func() time.Time { return now }
	handler := limiter.limit(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	request := func(token string) int {
		req := httptest.NewRequest(http.MethodPost, "/v1/lineages/renew", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer "+token)
		res := httptest.NewRecorder()
		handler.ServeHTTP(res, req)
		return res.Code
	}

	unknown := "923e4567-e89b-42d3-a456-426614174000"
	for index := 0; index < workspaceAPIInvalidLimit; index++ {
		if status := request(unknown); status != http.StatusUnauthorized {
			t.Fatalf("unregistered request %d returned %d", index, status)
		}
	}
	if status := request(unknown); status != http.StatusTooManyRequests {
		t.Fatalf("unregistered flood returned %d after the limit", status)
	}
	if len(limiter.registered) != 0 {
		t.Fatal("unregistered tokens allocated per-token limiter state")
	}
	if status := request(first.AgentToken); status != http.StatusNoContent {
		t.Fatalf("invalid-token flood suppressed a registered agent: %d", status)
	}
	for index := 1; index < workspaceAPITokenLimit; index++ {
		if status := request(first.AgentToken); status != http.StatusNoContent {
			t.Fatalf("registered request %d returned %d", index, status)
		}
	}
	if status := request(first.AgentToken); status != http.StatusTooManyRequests {
		t.Fatalf("registered flood returned %d after the limit", status)
	}
	if status := request(second.AgentToken); status != http.StatusNoContent {
		t.Fatalf("one registered agent suppressed another: %d", status)
	}
	now = now.Add(workspaceAPIRateWindow)
	if status := request(first.AgentToken); status != http.StatusNoContent {
		t.Fatalf("registered agent did not recover after its rate window: %d", status)
	}
}

func TestPublicWorkspaceAPIAdmitsRegisteredAgent(t *testing.T) {
	registration := validWorkspaceAgentRegistration()
	registry := registeredLimiterRegistry(t, registration)
	registry.validator = &fixedAgentValidator{}
	coordinator := &fixedCoordinator{key: "single-use"}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "ldap"}, coordinator)
	s.authenticator = registry
	s.agentRegistry = registry
	coordinatorServer := httptest.NewServer(coordinatorMux(s))
	defer coordinatorServer.Close()
	request := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(
		`{"cluster":"cell-eaws-lh1","machine":"ldap-dev"}`,
	))
	request.Header.Set("Authorization", "Bearer "+registration.AgentToken)
	response := httptest.NewRecorder()
	publicMuxWithUpstreams(s, http.NotFoundHandler(), reverseProxy(t, coordinatorServer.URL)).ServeHTTP(response, request)
	if response.Code != http.StatusOK || coordinator.issues != 1 {
		t.Fatalf("registered public enrollment returned %d: %s", response.Code, response.Body.String())
	}
}

func TestLoopbackCoordinatorAuthenticatesIndependently(t *testing.T) {
	registration := validWorkspaceAgentRegistration()
	publicRegistry := registeredLimiterRegistry(t, registration)
	coordinatorRegistry := registeredLimiterRegistry(t)
	coordinatorRegistry.validator = &fixedAgentValidator{}
	coordinator := &fixedCoordinator{key: "must-not-issue"}
	privateServer := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "ldap"}, coordinator)
	privateServer.authenticator = coordinatorRegistry
	privateServer.agentRegistry = coordinatorRegistry
	upstream := httptest.NewServer(coordinatorMux(privateServer))
	defer upstream.Close()

	request := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(
		`{"cluster":"cell-eaws-lh1","machine":"ldap-dev"}`,
	))
	request.Header.Set("Authorization", "Bearer "+registration.AgentToken)
	response := httptest.NewRecorder()
	publicMuxWithUpstreams(
		server{agentRegistry: publicRegistry},
		http.NotFoundHandler(),
		reverseProxy(t, upstream.URL),
	).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized || coordinator.issues != 0 {
		t.Fatalf("public admission bypassed coordinator authentication: status=%d issues=%d", response.Code, coordinator.issues)
	}
}

func TestLoopbackCoordinatorBoundsExpensiveOperations(t *testing.T) {
	registration := validWorkspaceAgentRegistration()
	registry := registeredLimiterRegistry(t, registration)
	registry.validator = &fixedAgentValidator{}
	coordinator := &blockingIssueCoordinator{
		entered: make(chan struct{}, coordinatorInFlightLimit),
		release: make(chan struct{}),
	}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "ldap"}, coordinator)
	s.authenticator = registry
	s.agentRegistry = registry
	handler := coordinatorMux(s)
	invoke := func() int {
		request := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(
			`{"cluster":"cell-eaws-lh1","machine":"ldap-dev"}`,
		))
		request.Header.Set("Authorization", "Bearer "+registration.AgentToken)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}

	completed := make(chan int, coordinatorInFlightLimit)
	for range coordinatorInFlightLimit {
		go func() { completed <- invoke() }()
		<-coordinator.entered
	}
	if status := invoke(); status != http.StatusServiceUnavailable {
		t.Fatalf("coordinator request above the in-flight cap returned %d", status)
	}
	close(coordinator.release)
	for range coordinatorInFlightLimit {
		if status := <-completed; status != http.StatusOK {
			t.Fatalf("admitted coordinator request returned %d", status)
		}
	}
}

func TestWorkspaceAPILimiterBoundsConcurrentAuthentication(t *testing.T) {
	request := validWorkspaceAgentRegistration()
	limiter := newWorkspaceAPILimiter(registeredLimiterRegistry(t, request))
	limiter.inFlight = make(chan struct{}, 1)
	entered := make(chan struct{})
	release := make(chan struct{})
	handler := limiter.limit(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(entered)
		<-release
		w.WriteHeader(http.StatusNoContent)
	}))
	invoke := func() int {
		req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer "+request.AgentToken)
		res := httptest.NewRecorder()
		handler.ServeHTTP(res, req)
		return res.Code
	}
	finished := make(chan int, 1)
	go func() { finished <- invoke() }()
	<-entered
	if status := invoke(); status != http.StatusServiceUnavailable {
		t.Fatalf("request above the in-flight cap returned %d", status)
	}
	close(release)
	if status := <-finished; status != http.StatusNoContent {
		t.Fatalf("admitted in-flight request returned %d", status)
	}
}

func TestPublicAPILimitDoesNotThrottleHeadscaleProxy(t *testing.T) {
	registry := registeredLimiterRegistry(t)
	headscale := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTeapot)
	})
	handler := publicMuxWithUpstreams(server{agentRegistry: registry}, headscale, http.NotFoundHandler())
	for index := 0; index <= workspaceAPIInvalidLimit; index++ {
		req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer 923e4567-e89b-42d3-a456-426614174000")
		res := httptest.NewRecorder()
		handler.ServeHTTP(res, req)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v1/node", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusTeapot {
		t.Fatalf("Kubernetes API flood throttled Headscale proxy traffic: %d", response.Code)
	}
}

func reverseProxy(t *testing.T, rawURL string) http.Handler {
	t.Helper()
	target, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	return httputil.NewSingleHostReverseProxy(target)
}

func registeredLimiterRegistry(t *testing.T, requests ...workspaceAgentRegistrationRequest) *workspaceAgentRegistry {
	t.Helper()
	bindings := make([]workspaceAgentBinding, 0, len(requests))
	for index, request := range requests {
		agentID := fixtureAgentID
		if index != 0 {
			agentID = "a23e4567-e89b-42d3-a456-426614174000"
		}
		bindings = append(bindings, workspaceAgentBinding{
			AgentID: agentID, TokenHash: workspaceAgentTokenHash(request.AgentToken),
			workspaceAgentMetadata: request.metadata(),
		})
	}
	path := t.TempDir() + "/agents.json"
	if err := writeWorkspaceAgentBindings(path, bindings); err != nil {
		t.Fatal(err)
	}
	return &workspaceAgentRegistry{mu: &sync.Mutex{}, path: path}
}
