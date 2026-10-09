// Runs public Headscale proxy, loopback enrollment coordinator, and private broker container modes.

package main

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/big"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	coordinatorCommandTimeout = 15 * time.Second
	coordinatorInFlightLimit  = 1
	maxCoordinatorEntities    = 10_000
	maxCoordinatorOutput      = 16 << 20
	maxBody                   = 16 << 10
	maxDNSRecords             = 10_000
	maxDNSRecordsBytes        = 4 << 20
	privateCoordinatorURL     = "http://127.0.0.1:8445"
	serverIdleTimeout         = 60 * time.Second
	serverReadHeaderTimeout   = 5 * time.Second
	serverReadTimeout         = 15 * time.Second
	serverWriteTimeout        = 30 * time.Second
	upstreamTimeout           = 10 * time.Second
)

var (
	dnsNameLabel     = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
	incarnationName  = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,62}$`)
	label            = regexp.MustCompile(`^[a-z][a-z0-9-]{1,61}[a-z0-9]$`)
	lineageName      = regexp.MustCompile(`^(?:[0-9a-f-]{36}-[0-9]{10,}|[a-z][a-z0-9-]{1,61}[a-z0-9]|[0-9a-f]{40})$`)
	loginName        = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,31}$`)
	snapshotSelector = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	userID           = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
)

type request struct {
	Cluster string `json:"cluster"`
	Machine string `json:"machine"`
	IPv4    string `json:"ipv4,omitempty"`
}

type bindRequest struct {
	OwnerID string `json:"owner_id"`
}

type coderUser struct {
	ID                string `json:"id"`
	Email             string `json:"email"`
	Groups            []string
	Issuer            string
	Subject           string
	PreferredUsername string
}

type oidcIdentity struct {
	Issuer            string
	Subject           string
	PreferredUsername string
}

type identity struct {
	AgentID         string
	Cell            string
	Incarnation     string
	Lineage         string
	ParentLineage   string
	ParentSnapshot  string
	IsRoot          bool
	Machine         string
	SubjectID       string
	OIDCIssuer      string
	OIDCSubject     string
	IdPUsername     string
	Team            string
	Workspace       string
	WorkspaceID     string
	WorkspaceVolume string
}

type record struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	Value string `json:"value"`
}

type authenticator interface {
	authenticate(context.Context, string) (identity, error)
}

type tokenVerifier interface {
	verify(context.Context, string) (oidcIdentity, error)
}

type ownerAuthenticator interface {
	authenticateOwner(context.Context, string) (coderUser, error)
}

type coordinator interface {
	issue(context.Context, identity, request) (string, error)
	register(context.Context, identity, request, netip.Addr) error
	revoke(context.Context, identity, request) error
	activeNodes(context.Context) ([]headscaleNode, error)
}

type server struct {
	authenticator       authenticator
	agentRegistry       *workspaceAgentRegistry
	coordinator         coordinator
	domain              string
	dnsPath             string
	bindingPath         string
	verifier            tokenVerifier
	ownerAuth           ownerAuthenticator
	provisionerAuth     provisionerTokenReviewer
	registrationLimiter *registrationRateLimiter
	bindingsMu          *sync.Mutex
	dnsMu               *sync.Mutex
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "init" {
		if err := initializeDNS(mustEnv("WORKSPACE_DNS_PATH")); err != nil {
			panic(err)
		}
		return
	}
	if len(os.Args) != 2 {
		panic("expected public, coordinator, or broker mode")
	}
	switch os.Args[1] {
	case "public":
		runPublicEnrollment()
	case "coordinator":
		runPrivateCoordinator()
	case "broker":
		runPrivateBroker()
	default:
		panic("expected public, coordinator, or broker mode")
	}
}

func newWorkspaceAgentRegistry(coder coderClient, client *http.Client) *workspaceAgentRegistry {
	return &workspaceAgentRegistry{
		mu:        &sync.Mutex{},
		path:      "/var/lib/workspace-enrollment/workspace-agent-bindings.json",
		pending:   map[string]*pendingWorkspaceAgent{},
		pendingMu: &sync.Mutex{},
		reader:    coder,
		validator: coderWebsocketValidator{baseURL: coder.baseURL, client: client, timeout: upstreamTimeout},
	}
}

func health(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusNoContent)
}

func boundedHTTPClient(timeout time.Duration) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = timeout
	transport.TLSHandshakeTimeout = timeout
	transport.IdleConnTimeout = serverIdleTimeout
	return &http.Client{
		Transport: transport,
		Timeout:   timeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func boundedHTTPServer(handler http.Handler) *http.Server {
	return boundedHTTPServerAt("0.0.0.0:8080", handler)
}

func boundedHTTPServerAt(address string, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              address,
		Handler:           handler,
		ReadHeaderTimeout: serverReadHeaderTimeout,
		ReadTimeout:       serverReadTimeout,
		WriteTimeout:      serverWriteTimeout,
		IdleTimeout:       serverIdleTimeout,
	}
}

func (s server) bind(w http.ResponseWriter, r *http.Request) {
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == "" {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	var req bindRequest
	decoder := json.NewDecoder(io.LimitReader(r.Body, maxBody))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil || !userID.MatchString(req.OwnerID) {
		http.Error(w, "invalid binding request", http.StatusBadRequest)
		return
	}
	owner, err := s.ownerAuth.authenticateOwner(r.Context(), r.Header.Get("Coder-Session-Token"))
	if err != nil || owner.ID != req.OwnerID {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	identity, err := s.verifier.verify(r.Context(), token)
	if err != nil || !loginName.MatchString(identity.PreferredUsername) ||
		owner.Issuer != identity.Issuer || owner.Subject != identity.Subject || owner.PreferredUsername != identity.PreferredUsername {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	bound, err := s.storeBinding(binding{
		Issuer:            identity.Issuer,
		Subject:           identity.Subject,
		PreferredUsername: identity.PreferredUsername,
		OwnerID:           req.OwnerID,
	})
	if err != nil {
		http.Error(w, "identity binding conflict", http.StatusConflict)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{
		"attested":           "true",
		"email":              owner.Email,
		"id":                 bound.OwnerID,
		"preferred_username": bound.PreferredUsername,
	})
}

func (s server) resolve(w http.ResponseWriter, r *http.Request) {
	owner, err := s.ownerAuth.authenticateOwner(r.Context(), r.Header.Get("Coder-Session-Token"))
	if err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	bound, err := s.bindingForOwner(owner.ID)
	if err != nil {
		http.Error(w, "identity binding not found", http.StatusNotFound)
		return
	}
	if owner.Issuer != bound.Issuer || owner.Subject != bound.Subject || owner.PreferredUsername != bound.PreferredUsername {
		http.Error(w, "identity binding conflict", http.StatusConflict)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{
		"attested":           "true",
		"email":              owner.Email,
		"id":                 bound.OwnerID,
		"preferred_username": bound.PreferredUsername,
	})
}

type oidcVerifier struct {
	issuer   string
	audience string
	client   *http.Client
	now      func() time.Time
}

func (v oidcVerifier) verify(ctx context.Context, token string) (oidcIdentity, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return oidcIdentity{}, errors.New("invalid JWT")
	}
	var header struct {
		Algorithm string `json:"alg"`
		KeyID     string `json:"kid"`
	}
	var claims struct {
		Issuer            string          `json:"iss"`
		Subject           string          `json:"sub"`
		Audience          json.RawMessage `json:"aud"`
		ExpiresAt         int64           `json:"exp"`
		PreferredUsername string          `json:"preferred_username"`
	}
	if decodeJWTPart(parts[0], &header) != nil || decodeJWTPart(parts[1], &claims) != nil || header.Algorithm != "RS256" || header.KeyID == "" {
		return oidcIdentity{}, errors.New("invalid JWT claims")
	}
	if claims.Issuer != v.issuer || claims.Subject == "" || claims.ExpiresAt <= v.now().Unix() || !hasAudience(claims.Audience, v.audience) {
		return oidcIdentity{}, errors.New("invalid JWT identity")
	}
	key, err := v.signingKey(ctx, header.KeyID)
	if err != nil {
		return oidcIdentity{}, err
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return oidcIdentity{}, err
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], signature); err != nil {
		return oidcIdentity{}, errors.New("invalid JWT signature")
	}
	return oidcIdentity{Issuer: claims.Issuer, Subject: claims.Subject, PreferredUsername: claims.PreferredUsername}, nil
}

func (v oidcVerifier) signingKey(ctx context.Context, keyID string) (*rsa.PublicKey, error) {
	var discovery struct {
		Issuer  string `json:"issuer"`
		JWKSURL string `json:"jwks_uri"`
	}
	if err := v.fetchJSON(ctx, strings.TrimRight(v.issuer, "/")+"/.well-known/openid-configuration", &discovery); err != nil || discovery.Issuer != v.issuer {
		return nil, errors.New("invalid OIDC discovery")
	}
	var keys struct {
		Keys []struct {
			KeyID     string `json:"kid"`
			KeyType   string `json:"kty"`
			Algorithm string `json:"alg"`
			Modulus   string `json:"n"`
			Exponent  string `json:"e"`
		} `json:"keys"`
	}
	if err := v.fetchJSON(ctx, discovery.JWKSURL, &keys); err != nil {
		return nil, err
	}
	for _, key := range keys.Keys {
		if key.KeyID != keyID || key.KeyType != "RSA" || key.Algorithm != "RS256" {
			continue
		}
		modulus, modulusErr := base64.RawURLEncoding.DecodeString(key.Modulus)
		exponent, exponentErr := base64.RawURLEncoding.DecodeString(key.Exponent)
		if modulusErr != nil || exponentErr != nil || len(exponent) == 0 || len(exponent) > 4 {
			return nil, errors.New("invalid OIDC signing key")
		}
		e := 0
		for _, octet := range exponent {
			e = e<<8 | int(octet)
		}
		return &rsa.PublicKey{N: new(big.Int).SetBytes(modulus), E: e}, nil
	}
	return nil, errors.New("OIDC signing key not found")
}

func (v oidcVerifier) fetchJSON(ctx context.Context, endpoint string, destination any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}
	res, err := v.client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("OIDC endpoint returned %s", res.Status)
	}
	return json.NewDecoder(io.LimitReader(res.Body, maxBody)).Decode(destination)
}

func decodeJWTPart(part string, destination any) error {
	decoded, err := base64.RawURLEncoding.DecodeString(part)
	if err != nil {
		return err
	}
	return json.Unmarshal(decoded, destination)
}

func hasAudience(raw json.RawMessage, expected string) bool {
	var single string
	if json.Unmarshal(raw, &single) == nil {
		return single == expected
	}
	var multiple []string
	if json.Unmarshal(raw, &multiple) != nil {
		return false
	}
	for _, audience := range multiple {
		if audience == expected {
			return true
		}
	}
	return false
}

func initializeDNS(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	_, err := os.Stat(path)
	if err == nil {
		_, err = readRecords(path)
		return err
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return writeRecords(path, []record{})
}

func (s server) enroll(w http.ResponseWriter, r *http.Request) {
	id, req, err := s.authorize(r)
	if err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	key, err := s.coordinator.issue(r.Context(), id, req)
	if err != nil {
		http.Error(w, "enrollment failed", http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"authKey": key, "hostname": s.hostname(req)})
}

func (s server) register(w http.ResponseWriter, r *http.Request) {
	id, req, err := s.authorize(r)
	if err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	ip, err := netip.ParseAddr(req.IPv4)
	tailnet := netip.MustParsePrefix("100.64.0.0/10")
	if err != nil || !ip.Is4() || !tailnet.Contains(ip) {
		http.Error(w, "invalid tailnet address", http.StatusBadRequest)
		return
	}
	s.dnsMu.Lock()
	defer s.dnsMu.Unlock()
	if err := s.coordinator.register(r.Context(), id, req, ip); err != nil {
		http.Error(w, "node ownership mismatch", http.StatusForbidden)
		return
	}
	if err := replaceRecord(s.dnsPath, record{Name: s.hostname(req), Type: "A", Value: ip.String()}); err != nil {
		http.Error(w, "DNS update failed", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s server) revoke(w http.ResponseWriter, r *http.Request) {
	id, req, err := s.authorize(r)
	if err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if req.IPv4 == "" {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	ip, err := netip.ParseAddr(req.IPv4)
	tailnet := netip.MustParsePrefix("100.64.0.0/10")
	if err != nil || !ip.Is4() || !tailnet.Contains(ip) {
		http.Error(w, "invalid tailnet address", http.StatusBadRequest)
		return
	}
	s.dnsMu.Lock()
	defer s.dnsMu.Unlock()
	if err := s.coordinator.revoke(r.Context(), id, req); err != nil {
		http.Error(w, "revoke failed", http.StatusBadGateway)
		return
	}
	if err := removeRecordValue(s.dnsPath, s.hostname(req), ip.String()); err != nil {
		http.Error(w, "DNS update failed", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func decodeStrictJSON(reader io.Reader, destination any) error {
	decoder := json.NewDecoder(io.LimitReader(reader, maxBody))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return errors.New("request contains trailing JSON")
	}
	return nil
}

func (s server) authenticateAgent(ctx context.Context, authorization string) (identity, error) {
	token := strings.TrimPrefix(authorization, "Bearer ")
	if token == "" {
		return identity{}, errors.New("missing agent token")
	}
	id, err := s.authenticator.authenticate(ctx, token)
	if err != nil || !userID.MatchString(id.SubjectID) {
		return identity{}, errors.New("invalid agent identity")
	}
	bound, err := s.bindingForOwner(id.SubjectID)
	if err != nil || !loginName.MatchString(bound.PreferredUsername) {
		return identity{}, errors.New("identity is not bound")
	}
	id.OIDCIssuer = bound.Issuer
	id.OIDCSubject = bound.Subject
	id.IdPUsername = bound.PreferredUsername
	return id, nil
}

func (s server) authorize(r *http.Request) (identity, request, error) {
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == "" {
		return identity{}, request{}, errors.New("missing agent token")
	}
	var req request
	decoder := json.NewDecoder(io.LimitReader(r.Body, maxBody))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		return identity{}, request{}, err
	}
	if !label.MatchString(req.Cluster) || !label.MatchString(req.Machine) {
		return identity{}, request{}, errors.New("invalid name")
	}
	id, err := s.authenticateAgent(r.Context(), "Bearer "+token)
	if err != nil || id.Cell != req.Cluster || id.Machine != req.Machine {
		return identity{}, request{}, errors.New("identity mismatch")
	}
	return id, req, nil
}

func (s server) hostname(req request) string { return req.Machine + "." + req.Cluster + "." + s.domain }

type commandCoordinator struct {
	binary  string
	config  string
	keyMu   *sync.Mutex
	keyPath string
	now     func() time.Time
	run     func(context.Context, ...string) ([]byte, error)
}

func (c commandCoordinator) revoke(ctx context.Context, id identity, req request) error {
	ip, err := netip.ParseAddr(req.IPv4)
	if err != nil {
		return err
	}
	userName, err := c.userName(ctx, id)
	if err != nil {
		return err
	}
	nodes, err := c.nodes(ctx, userName)
	if err != nil {
		return err
	}
	for _, node := range nodes {
		if node.Hostname == req.Machine && node.IPv4 == ip.String() {
			_, err := c.execute(ctx, "nodes", "delete", "--identifier", fmt.Sprint(node.ID), "--force")
			return err
		}
	}
	return nil
}

func (c commandCoordinator) register(ctx context.Context, id identity, req request, ip netip.Addr) error {
	userName, err := c.userName(ctx, id)
	if err != nil {
		return err
	}
	nodes, err := c.nodes(ctx, userName)
	if err != nil {
		return err
	}
	for _, node := range nodes {
		if node.Hostname == req.Machine && node.IPv4 == ip.String() {
			return nil
		}
	}
	return errors.New("registered node does not belong to the workspace owner")
}

type headscaleNode struct {
	ID        uint64
	GivenName string
	Hostname  string
	IPv4      string
}

type headscaleNodeDocument struct {
	ID          uint64   `json:"id"`
	GivenName   string   `json:"given_name"`
	IPAddresses []string `json:"ip_addresses"`
	Name        string   `json:"name"`
}

type headscaleUser struct {
	ID         uint64 `json:"id"`
	Name       string `json:"name"`
	ProviderID string `json:"provider_id"`
}

func (c commandCoordinator) nodes(ctx context.Context, user string) ([]headscaleNode, error) {
	out, err := c.execute(ctx, "nodes", "list", "--user", user, "--output", "json")
	if err != nil {
		return nil, err
	}
	return parseHeadscaleNodes(out)
}

func (c commandCoordinator) activeNodes(ctx context.Context) ([]headscaleNode, error) {
	out, err := c.execute(ctx, "nodes", "list", "--output", "json")
	if err != nil {
		return nil, err
	}
	return parseHeadscaleNodes(out)
}

func parseHeadscaleNodes(data []byte) ([]headscaleNode, error) {
	var documents []headscaleNodeDocument
	if err := json.Unmarshal(data, &documents); err != nil {
		return nil, err
	}
	if len(documents) > maxCoordinatorEntities {
		return nil, errors.New("Headscale returned too many nodes")
	}
	tailnet := netip.MustParsePrefix("100.64.0.0/10")
	nodes := make([]headscaleNode, 0, len(documents))
	ids := make(map[uint64]struct{}, len(documents))
	for _, document := range documents {
		if document.ID == 0 || !dnsNameLabel.MatchString(document.GivenName) ||
			!validRawHostname(document.Name) || len(document.IPAddresses) > 8 {
			return nil, errors.New("Headscale returned an invalid node")
		}
		if _, duplicate := ids[document.ID]; duplicate {
			return nil, errors.New("Headscale returned a duplicate node ID")
		}
		ids[document.ID] = struct{}{}
		var ipv4 netip.Addr
		for _, rawAddress := range document.IPAddresses {
			address, err := netip.ParseAddr(rawAddress)
			if err != nil {
				return nil, errors.New("Headscale returned an invalid node address")
			}
			if !address.Is4() {
				continue
			}
			if ipv4.IsValid() {
				return nil, errors.New("Headscale returned multiple IPv4 node addresses")
			}
			ipv4 = address
		}
		if !ipv4.IsValid() || !tailnet.Contains(ipv4) {
			return nil, errors.New("Headscale returned no valid tailnet IPv4 node address")
		}
		nodes = append(nodes, headscaleNode{
			ID: document.ID, GivenName: document.GivenName, Hostname: document.Name, IPv4: ipv4.String(),
		})
	}
	return nodes, nil
}

func validRawHostname(hostname string) bool {
	if hostname == "" || len(hostname) > 255 {
		return false
	}
	for _, character := range []byte(hostname) {
		if character < 0x20 || character > 0x7e {
			return false
		}
	}
	return true
}

func (c commandCoordinator) userID(ctx context.Context, id identity) (string, error) {
	user, err := c.user(ctx, id)
	if err != nil {
		return "", err
	}
	return fmt.Sprint(user.ID), nil
}

func (c commandCoordinator) userName(ctx context.Context, id identity) (string, error) {
	user, err := c.user(ctx, id)
	if err != nil {
		return "", err
	}
	return user.Name, nil
}

func (c commandCoordinator) user(ctx context.Context, id identity) (*headscaleUser, error) {
	out, err := c.execute(ctx, "users", "list", "--output", "json")
	if err != nil {
		return nil, err
	}
	var users []headscaleUser
	if err := json.Unmarshal(out, &users); err != nil {
		return nil, err
	}
	if len(users) > maxCoordinatorEntities {
		return nil, errors.New("Headscale returned too many users")
	}
	providerID, err := headscaleProviderID(id.OIDCIssuer, id.OIDCSubject)
	if err != nil {
		return nil, err
	}
	var match *headscaleUser
	ids := make(map[uint64]struct{}, len(users))
	for _, user := range users {
		if user.ID == 0 || user.Name == "" || len(user.Name) > 255 || len(user.ProviderID) > 2048 {
			return nil, errors.New("Headscale returned an invalid user")
		}
		if _, duplicate := ids[user.ID]; duplicate {
			return nil, errors.New("Headscale returned a duplicate user ID")
		}
		ids[user.ID] = struct{}{}
		if user.Name != id.IdPUsername || user.ProviderID != providerID {
			continue
		}
		if match != nil {
			return nil, errors.New("multiple Headscale OIDC users match the attested identity")
		}
		user := user
		match = &user
	}
	if match == nil {
		return nil, errors.New("Headscale OIDC user is not enrolled")
	}
	return match, nil
}

func (c commandCoordinator) execute(ctx context.Context, arguments ...string) ([]byte, error) {
	if c.run != nil {
		return c.run(ctx, arguments...)
	}
	commandCtx, cancel := context.WithTimeout(ctx, coordinatorCommandTimeout)
	defer cancel()
	command := exec.CommandContext(commandCtx, c.binary, append([]string{"--config", c.config}, arguments...)...)
	stdout, err := command.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := command.Start(); err != nil {
		return nil, err
	}
	output, readErr := readBounded(stdout, maxCoordinatorOutput)
	if readErr != nil {
		_ = command.Process.Kill()
		_ = command.Wait()
		return nil, readErr
	}
	if err := command.Wait(); err != nil {
		return nil, err
	}
	return output, nil
}

func readBounded(reader io.Reader, limit int64) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errors.New("input exceeds the configured limit")
	}
	return data, nil
}

func headscaleProviderID(issuer, subject string) (string, error) {
	if issuer == "" || subject == "" {
		return "", errors.New("OIDC provider identity is incomplete")
	}
	identifier := strings.TrimSpace(strings.TrimSuffix(issuer, "/") + "/" + strings.TrimPrefix(subject, "/"))
	parsed, err := url.Parse(identifier)
	if err != nil || parsed.Scheme == "" {
		return "", errors.New("OIDC provider identity is invalid")
	}
	parts := strings.FieldsFunc(parsed.Path, func(r rune) bool { return r == '/' })
	cleaned := make([]string, 0, len(parts))
	for _, part := range parts {
		if part = strings.TrimSpace(part); part != "" {
			cleaned = append(cleaned, part)
		}
	}
	parsed.Path = ""
	if len(cleaned) != 0 {
		parsed.Path = "/" + strings.Join(cleaned, "/")
	}
	parsed.Scheme = strings.ToLower(parsed.Scheme)
	return parsed.String(), nil
}

func replaceRecord(path string, next record) error {
	records, err := readRecords(path)
	if err != nil {
		return err
	}
	filtered := make([]record, 0, len(records)+1)
	for _, r := range records {
		if r.Name != next.Name {
			filtered = append(filtered, r)
		}
	}
	return writeRecords(path, append(filtered, next))
}

func (s server) replaceDNSRecord(next record) error {
	s.dnsMu.Lock()
	defer s.dnsMu.Unlock()
	return replaceRecord(s.dnsPath, next)
}

func removeRecord(path, name string) error {
	records, err := readRecords(path)
	if err != nil {
		return err
	}
	filtered := make([]record, 0, len(records))
	for _, r := range records {
		if r.Name != name {
			filtered = append(filtered, r)
		}
	}
	return writeRecords(path, filtered)
}

func removeRecordValue(path, name, value string) error {
	records, err := readRecords(path)
	if err != nil {
		return err
	}
	filtered := make([]record, 0, len(records))
	for _, r := range records {
		if r.Name != name || r.Value != value {
			filtered = append(filtered, r)
		}
	}
	return writeRecords(path, filtered)
}

func (s server) removeDNSRecord(name string) error {
	s.dnsMu.Lock()
	defer s.dnsMu.Unlock()
	return removeRecord(s.dnsPath, name)
}

func (s server) reconcileDNSLoop(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := s.reconcileDNS(ctx); err != nil {
				log.Printf("reconcile workspace DNS: %v", err)
			}
		}
	}
}

func (s server) reconcileDNS(ctx context.Context) error {
	s.dnsMu.Lock()
	defer s.dnsMu.Unlock()
	nodes, err := s.coordinator.activeNodes(ctx)
	if err != nil {
		return err
	}
	active := make(map[string]map[string]struct{}, len(nodes))
	for _, node := range nodes {
		addresses := active[node.Hostname]
		if addresses == nil {
			addresses = make(map[string]struct{})
			active[node.Hostname] = addresses
		}
		addresses[node.IPv4] = struct{}{}
	}
	records, err := readRecords(s.dnsPath)
	if err != nil {
		return err
	}
	retained := make([]record, 0, len(records))
	changed := false
	for _, current := range records {
		machine, managed := workspaceRecordMachine(current.Name, s.domain)
		_, activeAddress := active[machine][current.Value]
		if !managed || activeAddress {
			retained = append(retained, current)
			continue
		}
		changed = true
	}
	if !changed {
		return nil
	}
	return writeRecords(s.dnsPath, retained)
}

func workspaceRecordMachine(name, domain string) (string, bool) {
	prefix, found := strings.CutSuffix(name, "."+domain)
	if !found {
		return "", false
	}
	machine, cluster, found := strings.Cut(prefix, ".")
	return machine, found && !strings.Contains(cluster, ".") && label.MatchString(machine) && label.MatchString(cluster)
}

func readRecords(path string) ([]record, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := readBounded(file, maxDNSRecordsBytes)
	if err != nil {
		return nil, err
	}
	var records []record
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&records); err != nil {
		return nil, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, errors.New("workspace DNS contains trailing data")
	}
	if err := validateRecords(records); err != nil {
		return nil, err
	}
	return records, nil
}

func writeRecords(path string, records []record) error {
	if err := validateRecords(records); err != nil {
		return err
	}
	return writeDurableJSON(path, records, maxDNSRecordsBytes)
}

func validateRecords(records []record) error {
	if len(records) > maxDNSRecords {
		return errors.New("workspace DNS contains too many records")
	}
	names := make(map[string]struct{}, len(records))
	tailnet := netip.MustParsePrefix("100.64.0.0/10")
	for _, current := range records {
		if current.Type != "A" || !validDNSName(current.Name) {
			return errors.New("workspace DNS contains an invalid record")
		}
		address, err := netip.ParseAddr(current.Value)
		if err != nil || !address.Is4() || !tailnet.Contains(address) {
			return errors.New("workspace DNS contains an invalid IPv4 address")
		}
		if _, duplicate := names[current.Name]; duplicate {
			return errors.New("workspace DNS contains a duplicate name")
		}
		names[current.Name] = struct{}{}
	}
	return nil
}

func validDNSName(name string) bool {
	if name == "" || len(name) > 253 || strings.HasSuffix(name, ".") {
		return false
	}
	parts := strings.Split(name, ".")
	if len(parts) < 2 {
		return false
	}
	for _, part := range parts {
		if !dnsNameLabel.MatchString(part) {
			return false
		}
	}
	return true
}

func mustEnv(name string) string {
	value := os.Getenv(name)
	if value == "" {
		panic(name + " is required")
	}
	return value
}

