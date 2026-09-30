// Tests workspace enrollment authentication, naming conventions, and dynamic DNS records.

package main

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"maps"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	fixtureAgentID     = "223e4567-e89b-42d3-a456-426614174000"
	fixtureUserID      = "8826ee2e-7933-4665-aef2-2393f84a0d05"
	fixtureWorkspaceID = "123e4567-e89b-42d3-a456-426614174000"
)

type fixedAuthenticator struct {
	identity identity
	err      error
}

type fixedOwnerAuthenticator struct {
	owner coderUser
	err   error
}

func TestBoundedHTTPServerSetsConnectionDeadlines(t *testing.T) {
	server := boundedHTTPServer(http.NewServeMux())
	if server.ReadHeaderTimeout <= 0 || server.ReadTimeout <= 0 ||
		server.WriteTimeout <= 0 || server.IdleTimeout <= 0 {
		t.Fatal("enrollment server omitted a connection deadline")
	}
}

func (a fixedOwnerAuthenticator) authenticateOwner(context.Context, string) (coderUser, error) {
	return a.owner, a.err
}

type fixedTokenVerifier struct {
	identity oidcIdentity
	err      error
}

func (v fixedTokenVerifier) verify(context.Context, string) (oidcIdentity, error) {
	return v.identity, v.err
}

func (a fixedAuthenticator) authenticate(context.Context, string) (identity, error) {
	return a.identity, a.err
}

type fixedCoordinator struct {
	key     string
	issues  int
	revokes int
	nodes   []headscaleNode
}

type serialCoordinator struct {
	registerStarted chan struct{}
	releaseRegister chan struct{}
	activeStarted   chan struct{}
}

func (c *serialCoordinator) issue(context.Context, identity, request) (string, error) {
	return "", nil
}

func (c *serialCoordinator) register(context.Context, identity, request, netip.Addr) error {
	close(c.registerStarted)
	<-c.releaseRegister
	return nil
}

func (c *serialCoordinator) revoke(context.Context, identity, request) error { return nil }

func (c *serialCoordinator) activeNodes(context.Context) ([]headscaleNode, error) {
	close(c.activeStarted)
	return []headscaleNode{{GivenName: "alice-dev", Hostname: "alice-dev", IPv4: "100.64.1.2"}}, nil
}

func (c *fixedCoordinator) issue(context.Context, identity, request) (string, error) {
	c.issues++
	return c.key, nil
}
func (c *fixedCoordinator) register(context.Context, identity, request, netip.Addr) error { return nil }
func (c *fixedCoordinator) revoke(context.Context, identity, request) error               { c.revokes++; return nil }
func (c *fixedCoordinator) activeNodes(context.Context) ([]headscaleNode, error) {
	return c.nodes, nil
}

func boundServer(t *testing.T, id identity, coordinator coordinator) server {
	t.Helper()
	if id.Cell == "" {
		id.Cell = "cell-eaws-lh1"
	}
	path := filepath.Join(t.TempDir(), "bindings.json")
	if userID.MatchString(id.SubjectID) && loginName.MatchString(id.IdPUsername) {
		if err := writeBindings(path, []binding{{Issuer: "https://issuer.example", Subject: "dex-subject", PreferredUsername: id.IdPUsername, OwnerID: id.SubjectID}}); err != nil {
			t.Fatal(err)
		}
	}
	return server{
		authenticator: fixedAuthenticator{identity: id},
		coordinator:   coordinator,
		domain:        "c.unit.test",
		dnsPath:       filepath.Join(t.TempDir(), "records.json"),
		bindingPath:   path,
		bindingsMu:    &sync.Mutex{},
		dnsMu:         &sync.Mutex{},
	}
}

func TestEnrollmentBindsWorkspaceIdentity(t *testing.T) {
	coordinator := &fixedCoordinator{key: "single-use"}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "simonepri_ldap", Machine: "simonepri-dev", Workspace: "dev"}, coordinator)
	req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"simonepri-dev"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.enroll(res, req)
	if res.Code != http.StatusOK || !strings.Contains(res.Body.String(), `"hostname":"simonepri-dev.cell-eaws-lh1.c.unit.test"`) || coordinator.issues != 1 {
		t.Fatalf("unexpected enrollment: %d %s", res.Code, res.Body.String())
	}
}

func TestEnrollmentRejectsForeignWorkspace(t *testing.T) {
	coordinator := &fixedCoordinator{key: "must-not-issue"}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "alice", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"bob-dev"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.enroll(res, req)
	if res.Code != http.StatusUnauthorized || coordinator.issues != 0 {
		t.Fatalf("foreign workspace issued a key: %d", res.Code)
	}
}

func TestEnrollmentRejectsForeignCluster(t *testing.T) {
	coordinator := &fixedCoordinator{key: "must-not-issue"}
	s := boundServer(t, identity{Cell: "cell-eaws-lh1", SubjectID: fixtureUserID, IdPUsername: "alice", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{"cluster":"cell-gcp-euw4","machine":"alice-dev"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.enroll(res, req)
	if res.Code != http.StatusUnauthorized || coordinator.issues != 0 {
		t.Fatalf("foreign cluster issued a key: %d", res.Code)
	}
}

func TestEnrollmentRejectsUnsafeLoginName(t *testing.T) {
	coordinator := &fixedCoordinator{key: "must-not-issue"}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "Alice.Admin", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"alice-dev"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.enroll(res, req)
	if res.Code != http.StatusUnauthorized || coordinator.issues != 0 {
		t.Fatalf("unsafe login name issued a key: %d", res.Code)
	}
}

func TestEnrollmentRejectsInvalidCoderOwnerID(t *testing.T) {
	coordinator := &fixedCoordinator{key: "must-not-issue"}
	s := boundServer(t, identity{SubjectID: "editable-name", IdPUsername: "alice", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	req := httptest.NewRequest(http.MethodPost, "/v1/enroll", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"alice-dev"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.enroll(res, req)
	if res.Code != http.StatusUnauthorized || coordinator.issues != 0 {
		t.Fatalf("invalid Coder owner ID issued a key: %d", res.Code)
	}
}

func TestCoderAuthenticatorVerifiesOwnerSession(t *testing.T) {
	coder := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Coder-Session-Token") != "owner-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/v2/users/me":
			_, _ = w.Write([]byte(`{"id":"` + fixtureUserID + `","email":"ldap@example.com","username":"edited-name"}`))
		case "/api/v2/users/oidc-claims":
			_, _ = w.Write([]byte(`{"claims":{"iss":"https://dex.example","sub":"dex-subject","preferred_username":"ldap_user","groups":["team:examples"]}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer coder.Close()

	owner, err := (coderClient{baseURL: coder.URL, client: coder.Client()}).authenticateOwner(context.Background(), "owner-token")
	if err != nil || owner.ID != fixtureUserID || owner.Email != "ldap@example.com" ||
		owner.Issuer != "https://dex.example" || owner.Subject != "dex-subject" || owner.PreferredUsername != "ldap_user" ||
		len(owner.Groups) != 1 || owner.Groups[0] != "team:examples" {
		t.Fatalf("unexpected Coder owner: %#v %v", owner, err)
	}
}

func TestBindingPersistsOnlyImmutableIdentityAndRejectsReplay(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bindings.json")
	s := server{
		bindingPath: path,
		verifier: fixedTokenVerifier{identity: oidcIdentity{
			Issuer: "https://dex.example", Subject: "dex-subject", PreferredUsername: "ldap_user",
		}},
		ownerAuth: fixedOwnerAuthenticator{owner: coderUser{
			ID: fixtureUserID, Email: "ldap@example.com", Issuer: "https://dex.example", Subject: "dex-subject", PreferredUsername: "ldap_user",
		}},
		bindingsMu: &sync.Mutex{},
	}
	request := func() *http.Request {
		req := httptest.NewRequest(http.MethodPost, "/v1/bind", strings.NewReader(`{"owner_id":"`+fixtureUserID+`"}`))
		req.Header.Set("Authorization", "Bearer raw-oidc-token")
		req.Header.Set("Coder-Session-Token", "owner-session-token")
		return req
	}
	res := httptest.NewRecorder()
	s.bind(res, request())
	if res.Code != http.StatusOK || !strings.Contains(res.Body.String(), `"attested":"true"`) ||
		!strings.Contains(res.Body.String(), `"preferred_username":"ldap_user"`) ||
		!strings.Contains(res.Body.String(), `"principal_id":"`+snapshotPrincipalID("https://dex.example", "dex-subject")+`"`) {
		t.Fatalf("unexpected binding response: %d %s", res.Code, res.Body.String())
	}
	stored, err := os.ReadFile(path)
	if err != nil || strings.Contains(string(stored), "raw-oidc-token") || strings.Contains(string(stored), "owner-session-token") {
		t.Fatalf("binding persisted a bearer token: %s %v", stored, err)
	}
	res = httptest.NewRecorder()
	s.bind(res, request())
	if res.Code != http.StatusConflict {
		t.Fatalf("replayed binding returned %d", res.Code)
	}
}

func TestBindingRequiresCoderSessionOwner(t *testing.T) {
	s := server{
		bindingPath: filepath.Join(t.TempDir(), "bindings.json"),
		verifier: fixedTokenVerifier{identity: oidcIdentity{
			Issuer: "https://dex.example", Subject: "dex-subject", PreferredUsername: "ldap_user",
		}},
		ownerAuth:  fixedOwnerAuthenticator{owner: coderUser{ID: "3a68b0be-8f85-42d4-b7b8-f231ea42ca00", Email: "other@example.com"}},
		bindingsMu: &sync.Mutex{},
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/bind", strings.NewReader(`{"owner_id":"`+fixtureUserID+`"}`))
	req.Header.Set("Authorization", "Bearer raw-oidc-token")
	req.Header.Set("Coder-Session-Token", "foreign-session-token")
	res := httptest.NewRecorder()
	s.bind(res, req)
	if res.Code != http.StatusUnauthorized {
		t.Fatalf("foreign Coder session returned %d", res.Code)
	}
}

func TestResolveRequiresBoundOIDCClaims(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bindings.json")
	bound := binding{Issuer: "https://dex.example", Subject: "dex-subject", PreferredUsername: "ldap_user", OwnerID: fixtureUserID}
	if err := writeBindings(path, []binding{bound}); err != nil {
		t.Fatal(err)
	}
	s := server{
		bindingPath: path,
		ownerAuth: fixedOwnerAuthenticator{owner: coderUser{
			ID: fixtureUserID, Email: "ldap@example.com", Issuer: bound.Issuer, Subject: bound.Subject, PreferredUsername: bound.PreferredUsername,
		}},
		bindingsMu: &sync.Mutex{},
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/resolve", nil)
	req.Header.Set("Coder-Session-Token", "owner-session-token")
	res := httptest.NewRecorder()
	s.resolve(res, req)
	if res.Code != http.StatusOK || !strings.Contains(res.Body.String(), `"preferred_username":"ldap_user"`) ||
		!strings.Contains(res.Body.String(), `"principal_id":"`+snapshotPrincipalID(bound.Issuer, bound.Subject)+`"`) {
		t.Fatalf("unexpected resolve response: %d %s", res.Code, res.Body.String())
	}

	for name, owner := range map[string]coderUser{
		"foreign owner": {
			ID: "3a68b0be-8f85-42d4-b7b8-f231ea42ca00", Email: "other@example.com", Issuer: bound.Issuer, Subject: bound.Subject, PreferredUsername: bound.PreferredUsername,
		},
		"issuer": {
			ID: fixtureUserID, Email: "ldap@example.com", Issuer: "https://other.example", Subject: bound.Subject, PreferredUsername: bound.PreferredUsername,
		},
		"preferred username": {
			ID: fixtureUserID, Email: "ldap@example.com", Issuer: bound.Issuer, Subject: bound.Subject, PreferredUsername: "changed_user",
		},
		"subject": {
			ID: fixtureUserID, Email: "ldap@example.com", Issuer: bound.Issuer, Subject: "different-subject", PreferredUsername: bound.PreferredUsername,
		},
	} {
		t.Run(name, func(t *testing.T) {
			s.ownerAuth = fixedOwnerAuthenticator{owner: owner}
			res := httptest.NewRecorder()
			s.resolve(res, req)
			want := http.StatusConflict
			if name == "foreign owner" {
				want = http.StatusNotFound
			}
			if res.Code != want {
				t.Fatalf("conflicting claims returned %d, want %d", res.Code, want)
			}
		})
	}
}

func TestOIDCVerifierChecksSignatureIssuerAudienceAndExpiry(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	var issuer string
	oidc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			_ = json.NewEncoder(w).Encode(map[string]string{"issuer": issuer, "jwks_uri": issuer + "/keys"})
		case "/keys":
			_ = json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]string{{
				"kid": "fixture", "kty": "RSA", "alg": "RS256",
				"n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()),
				"e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes()),
			}}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer oidc.Close()
	issuer = oidc.URL
	now := time.Unix(2_000_000_000, 0)
	verifier := oidcVerifier{issuer: issuer, audience: "coder", client: oidc.Client(), now: func() time.Time { return now }}

	claims := map[string]any{"iss": issuer, "sub": "dex-subject", "aud": "coder", "exp": now.Add(time.Minute).Unix(), "preferred_username": "ldap_user"}
	token := signedToken(t, key, claims)
	identity, err := verifier.verify(context.Background(), token)
	if err != nil || identity.PreferredUsername != "ldap_user" {
		t.Fatalf("valid token rejected: %#v %v", identity, err)
	}
	for name, change := range map[string]func(map[string]any){
		"audience": func(values map[string]any) { values["aud"] = "headscale" },
		"expiry":   func(values map[string]any) { values["exp"] = now.Add(-time.Minute).Unix() },
		"issuer":   func(values map[string]any) { values["iss"] = "https://other.example" },
	} {
		t.Run(name, func(t *testing.T) {
			invalid := maps.Clone(claims)
			change(invalid)
			if _, err := verifier.verify(context.Background(), signedToken(t, key, invalid)); err == nil {
				t.Fatal("invalid token accepted")
			}
		})
	}
	parts := strings.Split(token, ".")
	replacement := "A"
	if strings.HasSuffix(parts[1], replacement) {
		replacement = "B"
	}
	parts[1] = parts[1][:len(parts[1])-1] + replacement
	tampered := strings.Join(parts, ".")
	if _, err := verifier.verify(context.Background(), tampered); err == nil {
		t.Fatal("invalid signature accepted")
	}
}

func signedToken(t *testing.T, key *rsa.PrivateKey, claims map[string]any) string {
	t.Helper()
	encode := func(value any) string {
		data, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		return base64.RawURLEncoding.EncodeToString(data)
	}
	signingInput := encode(map[string]string{"alg": "RS256", "kid": "fixture", "typ": "JWT"}) + "." + encode(claims)
	digest := sha256.Sum256([]byte(signingInput))
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func TestRegisterAcceptsOnlyTailnetAddress(t *testing.T) {
	path := filepath.Join(t.TempDir(), "records.json")
	coordinator := &fixedCoordinator{}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "alice", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	s.dnsPath = path
	for _, tc := range []struct {
		ip   string
		want int
	}{{"100.64.1.2", http.StatusNoContent}, {"203.0.113.1", http.StatusBadRequest}} {
		req := httptest.NewRequest(http.MethodPost, "/v1/register", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"alice-dev","ipv4":"`+tc.ip+`"}`))
		req.Header.Set("Authorization", "Bearer agent-token")
		res := httptest.NewRecorder()
		s.register(res, req)
		if res.Code != tc.want {
			t.Fatalf("%s: got %d want %d", tc.ip, res.Code, tc.want)
		}
	}
	data, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(data), "alice-dev.cell-eaws-lh1.c.unit.test") {
		t.Fatalf("record not written: %s %v", data, err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/revoke", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"alice-dev","ipv4":"100.64.1.2"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.revoke(res, req)
	data, err = os.ReadFile(path)
	if res.Code != http.StatusNoContent || coordinator.revokes != 1 || err != nil || string(data) != "[]\n" {
		t.Fatalf("record not revoked: status=%d revokes=%d data=%s error=%v", res.Code, coordinator.revokes, data, err)
	}
}

func TestRevokeWithoutRegisteredAddressChangesNothing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "records.json")
	coordinator := &fixedCoordinator{}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "alice", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	s.dnsPath = path
	if err := writeRecords(path, []record{{
		Name: "alice-dev.cell-eaws-lh1.c.unit.test", Type: "A", Value: "100.64.1.2",
	}}); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/revoke", strings.NewReader(`{"cluster":"cell-eaws-lh1","machine":"alice-dev"}`))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.revoke(res, req)
	records, err := readRecords(path)
	if res.Code != http.StatusNoContent || coordinator.revokes != 0 || err != nil || len(records) != 1 {
		t.Fatalf("incomplete revocation changed state: status=%d revokes=%d records=%#v error=%v", res.Code, coordinator.revokes, records, err)
	}
}

func TestReconcileDNSRemovesOnlyStaleManagedRecords(t *testing.T) {
	path := filepath.Join(t.TempDir(), "records.json")
	coordinator := &fixedCoordinator{nodes: []headscaleNode{{GivenName: "active-dev-1", Hostname: "active-dev", IPv4: "100.64.1.2"}}}
	s := boundServer(t, identity{}, coordinator)
	s.dnsPath = path
	if err := writeRecords(path, []record{
		{Name: "active-dev.cell-eaws-lh1.c.unit.test", Type: "A", Value: "100.64.1.2"},
		{Name: "stale-dev.cell-eaws-lh1.c.unit.test", Type: "A", Value: "100.64.1.3"},
		{Name: "service.example.net", Type: "A", Value: "100.64.1.4"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := s.reconcileDNS(context.Background()); err != nil {
		t.Fatal(err)
	}
	records, err := readRecords(path)
	if err != nil || len(records) != 2 || records[0].Name != "active-dev.cell-eaws-lh1.c.unit.test" || records[1].Name != "service.example.net" {
		t.Fatalf("unexpected reconciled records: %#v %v", records, err)
	}
}

func TestConcurrentDNSUpdatesPreserveEveryWorkspace(t *testing.T) {
	path := filepath.Join(t.TempDir(), "records.json")
	s := server{dnsPath: path, dnsMu: &sync.Mutex{}}
	const workspaceCount = 32
	errors := make(chan error, workspaceCount)
	var updates sync.WaitGroup
	for index := range workspaceCount {
		updates.Add(1)
		go func() {
			defer updates.Done()
			errors <- s.replaceDNSRecord(record{
				Name:  fmt.Sprintf("workspace-%d.cell-eaws-lh1.c.unit.test", index),
				Type:  "A",
				Value: fmt.Sprintf("100.64.1.%d", index+1),
			})
		}()
	}
	updates.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	records, err := readRecords(path)
	if err != nil || len(records) != workspaceCount {
		t.Fatalf("concurrent updates retained %d records: %v", len(records), err)
	}
}

func TestRegisterSerializesNodeSnapshotAndDNSWrite(t *testing.T) {
	coordinator := &serialCoordinator{
		registerStarted: make(chan struct{}),
		releaseRegister: make(chan struct{}),
		activeStarted:   make(chan struct{}),
	}
	s := boundServer(t, identity{SubjectID: fixtureUserID, IdPUsername: "alice", Machine: "alice-dev", Workspace: "dev"}, coordinator)
	registerDone := make(chan int, 1)
	go func() {
		req := httptest.NewRequest(http.MethodPost, "/v1/register", strings.NewReader(
			`{"cluster":"cell-eaws-lh1","machine":"alice-dev","ipv4":"100.64.1.2"}`,
		))
		req.Header.Set("Authorization", "Bearer agent-token")
		res := httptest.NewRecorder()
		s.register(res, req)
		registerDone <- res.Code
	}()
	<-coordinator.registerStarted
	reconcileDone := make(chan error, 1)
	go func() { reconcileDone <- s.reconcileDNS(context.Background()) }()
	select {
	case <-coordinator.activeStarted:
		t.Fatal("DNS reconciliation took a node snapshot during a registration update")
	case <-time.After(20 * time.Millisecond):
	}
	close(coordinator.releaseRegister)
	if status := <-registerDone; status != http.StatusNoContent {
		t.Fatalf("registration returned %d", status)
	}
	if err := <-reconcileDone; err != nil {
		t.Fatal(err)
	}
}

func TestInitializeDNSPreservesExistingRecords(t *testing.T) {
	path := filepath.Join(t.TempDir(), "workspace-dns", "records.json")
	if err := initializeDNS(path); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "[]\n" {
		t.Fatalf("unexpected initial records: %q %v", data, err)
	}
	want := []byte(`[{"name":"ldap-dev.cell-eaws-lh1.c.unit.test","type":"A","value":"100.64.1.2"}]` + "\n")
	if err := os.WriteFile(path, want, 0600); err != nil {
		t.Fatal(err)
	}
	if err := initializeDNS(path); err != nil {
		t.Fatal(err)
	}
	data, err = os.ReadFile(path)
	if err != nil || string(data) != string(want) {
		t.Fatalf("existing records changed: %q %v", data, err)
	}
}

func TestInitializeDNSRejectsCorruptExistingRecords(t *testing.T) {
	path := filepath.Join(t.TempDir(), "workspace-dns", "records.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("not-json\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := initializeDNS(path); err == nil {
		t.Fatal("corrupt persistent DNS state was accepted")
	}
}

func TestWorkspaceDNSStateIsBoundedAndStrict(t *testing.T) {
	validName := "ldap-dev.cell-eaws-lh1.c.unit.test"
	for _, testCase := range []struct {
		name string
		data string
	}{
		{name: "unknown field", data: `[{"name":"` + validName + `","type":"A","value":"100.64.1.2","ttl":60}]`},
		{name: "duplicate name", data: `[{"name":"` + validName + `","type":"A","value":"100.64.1.2"},{"name":"` + validName + `","type":"A","value":"100.64.1.3"}]`},
		{name: "invalid record name", data: `[{"name":"UPPER.example","type":"A","value":"100.64.1.2"}]`},
		{name: "invalid record type", data: `[{"name":"` + validName + `","type":"AAAA","value":"fd7a:115c:a1e0::2"}]`},
		{name: "invalid IPv4", data: `[{"name":"` + validName + `","type":"A","value":"not-an-address"}]`},
		{name: "address outside tailnet", data: `[{"name":"` + validName + `","type":"A","value":"192.0.2.1"}]`},
		{name: "trailing document", data: `[] {}`},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "records.json")
			if err := os.WriteFile(path, []byte(testCase.data), 0600); err != nil {
				t.Fatal(err)
			}
			if records, err := readRecords(path); err == nil {
				t.Fatalf("invalid DNS state was accepted: %#v", records)
			}
		})
	}

	t.Run("record count", func(t *testing.T) {
		records := make([]record, maxDNSRecords+1)
		for index := range records {
			records[index] = record{
				Name:  fmt.Sprintf("workspace-%d.example.net", index),
				Type:  "A",
				Value: fmt.Sprintf("100.64.%d.%d", index/250, index%250+1),
			}
		}
		data, err := json.Marshal(records)
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(t.TempDir(), "records.json")
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
		if records, err := readRecords(path); err == nil {
			t.Fatalf("oversized DNS record set was accepted: %d", len(records))
		}
	})

	t.Run("byte size", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "records.json")
		if err := os.WriteFile(path, []byte(strings.Repeat(" ", maxDNSRecordsBytes+1)), 0600); err != nil {
			t.Fatal(err)
		}
		if records, err := readRecords(path); err == nil {
			t.Fatalf("oversized DNS file was accepted: %d", len(records))
		}
	})
}

func TestCoordinatorOutputIsBounded(t *testing.T) {
	if data, err := readBounded(strings.NewReader(strings.Repeat("x", maxCoordinatorOutput+1)), maxCoordinatorOutput); err == nil {
		t.Fatalf("oversized coordinator output was accepted: %d", len(data))
	}
}

func TestCoordinatorParsesPinnedHeadscaleNodeAddresses(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "headscale")
	fixture := filepath.Join(directory, "nodes.json")
	argumentLog := filepath.Join(directory, "arguments")
	script := "#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$ARGUMENT_LOG\"\ncat \"$NODE_FIXTURE\"\n"
	if err := os.WriteFile(binary, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("ARGUMENT_LOG", argumentLog)
	t.Setenv("NODE_FIXTURE", fixture)
	coordinator := commandCoordinator{binary: binary, config: "/etc/headscale/config.yaml"}
	for _, testCase := range []struct {
		name          string
		document      string
		wantGivenName string
		wantHostname  string
		wantIPv4      string
		wantError     bool
	}{
		{
			name:          "one tailnet IPv4 plus IPv6",
			document:      `[{"id":7,"given_name":"ldap-dev-1","ip_addresses":["100.64.1.7","fd7a:115c:a1e0::7"],"name":"ldap-dev"}]`,
			wantGivenName: "ldap-dev-1",
			wantHostname:  "ldap-dev",
			wantIPv4:      "100.64.1.7",
		},
		{
			name:          "raw hostname outside DNS label grammar",
			document:      `[{"id":7,"given_name":"simones-macbook","ip_addresses":["100.64.1.8"],"name":"Simone's MacBook"}]`,
			wantGivenName: "simones-macbook",
			wantHostname:  "Simone's MacBook",
			wantIPv4:      "100.64.1.8",
		},
		{name: "empty node set", document: `[]`},
		{name: "missing hostname", document: `[{"id":7,"given_name":"ldap-dev","ip_addresses":["100.64.1.7"]}]`, wantError: true},
		{name: "non-ASCII hostname", document: `[{"id":7,"given_name":"cafe","ip_addresses":["100.64.1.7"],"name":"café"}]`, wantError: true},
		{name: "missing addresses", document: `[{"id":7,"given_name":"ldap-dev","ip_addresses":[],"name":"ldap-dev"}]`, wantError: true},
		{name: "IPv6 only", document: `[{"id":7,"given_name":"ldap-dev","ip_addresses":["fd7a:115c:a1e0::7"],"name":"ldap-dev"}]`, wantError: true},
		{name: "multiple IPv4", document: `[{"id":7,"given_name":"ldap-dev","ip_addresses":["100.64.1.7","100.64.1.8"],"name":"ldap-dev"}]`, wantError: true},
		{name: "IPv4 outside tailnet", document: `[{"id":7,"given_name":"ldap-dev","ip_addresses":["192.0.2.7"],"name":"ldap-dev"}]`, wantError: true},
		{name: "malformed address", document: `[{"id":7,"given_name":"ldap-dev","ip_addresses":["not-an-address"],"name":"ldap-dev"}]`, wantError: true},
		{name: "legacy ipv4 field", document: `[{"id":7,"given_name":"ldap-dev","ipv4":"100.64.1.7","name":"ldap-dev"}]`, wantError: true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if err := os.WriteFile(fixture, []byte(testCase.document), 0600); err != nil {
				t.Fatal(err)
			}
			nodes, err := coordinator.activeNodes(context.Background())
			if testCase.wantError {
				if err == nil {
					t.Fatalf("invalid node document accepted: %#v", nodes)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if testCase.wantIPv4 == "" {
				if len(nodes) != 0 {
					t.Fatalf("empty node document returned %#v", nodes)
				}
				return
			}
			if len(nodes) != 1 || nodes[0].IPv4 != testCase.wantIPv4 ||
				nodes[0].GivenName != testCase.wantGivenName || nodes[0].Hostname != testCase.wantHostname {
				t.Fatalf("parsed nodes = %#v", nodes)
			}
		})
	}
	arguments, err := os.ReadFile(argumentLog)
	if err != nil || !strings.Contains("\n"+string(arguments), "\nnodes\nlist\n--output\njson\n") {
		t.Fatalf("unexpected Headscale command: %q %v", arguments, err)
	}
}

func TestCoordinatorUsesRawHostnameAndAddressWithinValidatedOwner(t *testing.T) {
	var commands [][]string
	coordinator := commandCoordinator{run: func(_ context.Context, arguments ...string) ([]byte, error) {
		commands = append(commands, append([]string(nil), arguments...))
		if reflect.DeepEqual(arguments, []string{"nodes", "delete", "--identifier", "7", "--force"}) {
			return nil, nil
		}
		switch arguments[0] {
		case "users":
			return []byte(`[{"id":23,"name":"ldap_user","provider_id":"https://dex.example/subject-a"}]`), nil
		case "nodes":
			return []byte(`[{"id":6,"given_name":"ldap-dev","ip_addresses":["100.64.1.6"],"name":"ldap-dev"},{"id":7,"given_name":"ldap-dev-1","ip_addresses":["100.64.1.7","fd7a:115c:a1e0::7"],"name":"ldap-dev"}]`), nil
		default:
			return nil, fmt.Errorf("unexpected Headscale command: %v", arguments)
		}
	}}
	id := identity{
		OIDCIssuer:  "https://dex.example",
		OIDCSubject: "subject-a",
		IdPUsername: "ldap_user",
	}
	if err := coordinator.register(
		context.Background(), id, request{Machine: "ldap-dev"}, netip.MustParseAddr("100.64.1.7"),
	); err != nil {
		t.Fatal(err)
	}
	if err := coordinator.revoke(
		context.Background(), id, request{Machine: "ldap-dev", IPv4: "100.64.1.7"},
	); err != nil {
		t.Fatal(err)
	}
	want := [][]string{
		{"users", "list", "--output", "json"},
		{"nodes", "list", "--user", "ldap_user", "--output", "json"},
		{"users", "list", "--output", "json"},
		{"nodes", "list", "--user", "ldap_user", "--output", "json"},
		{"nodes", "delete", "--identifier", "7", "--force"},
	}
	if !reflect.DeepEqual(commands, want) {
		t.Fatalf("Headscale commands = %#v, want %#v", commands, want)
	}
}

func TestCoordinatorRejectsTooManyNodes(t *testing.T) {
	documents := make([]headscaleNodeDocument, maxCoordinatorEntities+1)
	data, err := json.Marshal(documents)
	if err != nil {
		t.Fatal(err)
	}
	if nodes, err := parseHeadscaleNodes(data); err == nil {
		t.Fatalf("oversized node inventory was accepted: %d", len(nodes))
	}
}

func TestCoordinatorSelectsExactOIDCProviderIdentity(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "headscale")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\ncat \"$2\"\n"), 0700); err != nil {
		t.Fatal(err)
	}
	id := identity{
		OIDCIssuer:  "https://dex.example",
		OIDCSubject: "subject-a",
		IdPUsername: "ldap_user",
	}
	for _, tc := range []struct {
		name    string
		users   string
		wantID  string
		wantErr bool
	}{
		{
			name:   "same name different subject",
			users:  `[{"id":17,"name":"ldap_user","provider_id":"https://dex.example/subject-b"},{"id":23,"name":"ldap_user","provider_id":"https://dex.example/subject-a"}]`,
			wantID: "23",
		},
		{
			name:    "no exact provider identity",
			users:   `[{"id":17,"name":"ldap_user","provider_id":"https://dex.example/subject-b"}]`,
			wantErr: true,
		},
		{
			name:    "duplicate exact provider identity",
			users:   `[{"id":17,"name":"ldap_user","provider_id":"https://dex.example/subject-a"},{"id":23,"name":"ldap_user","provider_id":"https://dex.example/subject-a"}]`,
			wantErr: true,
		},
		{
			name:    "provider identity with different name",
			users:   `[{"id":17,"name":"edited_name","provider_id":"https://dex.example/subject-a"}]`,
			wantErr: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			users := filepath.Join(t.TempDir(), "users.json")
			if err := os.WriteFile(users, []byte(tc.users), 0600); err != nil {
				t.Fatal(err)
			}
			got, err := (commandCoordinator{binary: binary, config: users}).userID(context.Background(), id)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("accepted Headscale user %q", got)
				}
				return
			}
			if err != nil || got != tc.wantID {
				t.Fatalf("user ID = %q, %v; want %q", got, err, tc.wantID)
			}
		})
	}

	users := make([]headscaleUser, maxCoordinatorEntities+1)
	for index := range users {
		users[index] = headscaleUser{ID: uint64(index + 1), Name: fmt.Sprintf("user-%d", index)}
	}
	data, err := json.Marshal(users)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "users.json")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if got, err := (commandCoordinator{binary: binary, config: path}).userID(context.Background(), id); err == nil {
		t.Fatalf("oversized user inventory was accepted as %q", got)
	}
}
