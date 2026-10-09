// Runs background daemons and HTTP reverse-proxy multiplexers for workspace enrollment roles.

package main

import (
	"context"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sync"
	"time"
)

func runPublicEnrollment() {
	client := boundedHTTPClient(upstreamTimeout)
	coderAuth := coderClient{baseURL: mustEnv("CODER_URL"), client: client}
	agentRegistry := newWorkspaceAgentRegistry(coderAuth, client)
	s := server{agentRegistry: agentRegistry}
	certificate := "/etc/headscale-tls/tls.crt"
	privateKey := "/etc/headscale-tls/tls.key"
	panic(boundedHTTPServerAt("0.0.0.0:8080", publicMux(s)).ListenAndServeTLS(certificate, privateKey))
}

func runPrivateCoordinator() {
	client := boundedHTTPClient(upstreamTimeout)
	coderAuth := coderClient{baseURL: mustEnv("CODER_URL"), client: client}
	agentRegistry := newWorkspaceAgentRegistry(coderAuth, client)
	keyPath := "/var/lib/workspace-enrollment/workspace-preauthkeys.json"
	if err := initializeWorkspacePreAuthKeys(keyPath); err != nil {
		panic(err)
	}
	coordinator := &commandCoordinator{
		binary:  "/ko-app/headscale",
		config:  "/etc/headscale/config.yaml",
		keyMu:   &sync.Mutex{},
		keyPath: keyPath,
		now:     time.Now,
	}
	s := server{
		authenticator: agentRegistry,
		agentRegistry: agentRegistry,
		coordinator:   coordinator,
		domain:        mustEnv("ACCESS_ALIAS_DOMAIN"),
		dnsPath:       mustEnv("WORKSPACE_DNS_PATH"),
		bindingPath:   "/var/lib/workspace-enrollment/workspace-owner-bindings.json",
		bindingsMu:    &sync.Mutex{},
		dnsMu:         &sync.Mutex{},
	}
	go s.reconcileDNSLoop(context.Background(), time.Minute)
	go coordinator.reconcilePreAuthKeysLoop(context.Background())
	panic(boundedHTTPServerAt("127.0.0.1:8445", coordinatorMux(s)).ListenAndServe())
}

func runPrivateBroker() {
	client := boundedHTTPClient(upstreamTimeout)
	coderAuth := coderClient{baseURL: mustEnv("CODER_URL"), client: client}
	kubernetesClient, err := kubernetesTokenReviewClient("/var/run/secrets/registration-kubernetes-api/ca.crt")
	if err != nil {
		panic(err)
	}
	agentRegistry := newWorkspaceAgentRegistry(coderAuth, client)
	s := server{
		authenticator: agentRegistry,
		agentRegistry: agentRegistry,
		bindingPath:   "/var/lib/workspace-enrollment/workspace-owner-bindings.json",
		verifier: oidcVerifier{
			issuer:   mustEnv("OIDC_ISSUER"),
			audience: mustEnv("OIDC_AUDIENCE"),
			client:   client,
			now:      time.Now,
		},
		ownerAuth: coderAuth,
		provisionerAuth: kubernetesTokenReviewer{
			client:         kubernetesClient,
			credentialPath: "/var/run/secrets/registration-kubernetes-api/token",
			endpoint:       "https://kubernetes.default.svc",
		},
		registrationLimiter: &registrationRateLimiter{},
		bindingsMu:          &sync.Mutex{},
	}
	certificate := "/etc/headscale-broker-tls/tls.crt"
	privateKey := "/etc/headscale-broker-tls/tls.key"
	panic(boundedHTTPServerAt("0.0.0.0:8443", registrationMux(s)).ListenAndServeTLS(certificate, privateKey))
}

func publicMux(s server) http.Handler {
	headscaleURL, _ := url.Parse("http://127.0.0.1:8081")
	coordinatorURL, _ := url.Parse(privateCoordinatorURL)
	return publicMuxWithUpstreams(
		s,
		httputil.NewSingleHostReverseProxy(headscaleURL),
		httputil.NewSingleHostReverseProxy(coordinatorURL),
	)
}

func publicMuxWithUpstreams(s server, headscale, coordinator http.Handler) http.Handler {
	mux := http.NewServeMux()
	workspaceAPI := newWorkspaceAPILimiter(s.agentRegistry)
	mux.HandleFunc("GET /health", health)
	mux.Handle("/v1/enroll", workspaceAPI.limit(coordinator))
	mux.Handle("/v1/register", workspaceAPI.limit(coordinator))
	mux.Handle("/v1/revoke", workspaceAPI.limit(coordinator))
	for _, privatePath := range []string{"/v1/bind", "/v1/resolve", registrationPath} {
		mux.HandleFunc(privatePath, http.NotFound)
	}
	mux.Handle("/", headscale)
	return mux
}

func coordinatorMux(s server) http.Handler {
	api := http.NewServeMux()
	api.HandleFunc("POST /v1/enroll", s.enroll)
	api.HandleFunc("POST /v1/register", s.register)
	api.HandleFunc("POST /v1/revoke", s.revoke)
	limiter := newWorkspaceAPILimiter(s.agentRegistry)
	limiter.inFlight = make(chan struct{}, coordinatorInFlightLimit)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", health)
	mux.Handle("/v1/", limiter.limit(api))
	return mux
}

func registrationMux(s server) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", health)
	mux.HandleFunc("POST /v1/bind", s.bind)
	mux.HandleFunc("POST /v1/resolve", s.resolve)
	mux.HandleFunc("POST "+registrationPath, s.registerWorkspaceAgent)
	return mux
}
