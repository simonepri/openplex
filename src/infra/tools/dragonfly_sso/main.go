// Serves Dragonfly Console through a constrained OIDC-to-Manager bridge.

package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	defaultManagerHost     = "dragonfly-manager"
	defaultManagerPort     = 8080
	defaultOAuthCookieName = "_dragonfly_console"
	rootUserID             = 1
	sessionDuration        = 15 * time.Minute
	sessionSeconds         = 15 * 60
	ssoRole                = "console-readonly"
)

type permissionPair struct {
	resource string
	action   string
}

var (
	expectedPermissions = []permissionPair{
		{"audits", "read"},
		{"clusters", "read"},
		{"jobs", "read"},
		{"peers", "read"},
		{"persistent-cache-tasks", "read"},
		{"scheduler-clusters", "read"},
		{"scheduler-features", "read"},
		{"schedulers", "read"},
		{"seed-peer-clusters", "read"},
		{"seed-peers", "read"},
		{"users", "read"},
	}

	forwardedHeaders = map[string]bool{
		"accept":            true,
		"accept-encoding":   true,
		"accept-language":   true,
		"if-modified-since": true,
		"if-none-match":     true,
		"range":             true,
		"user-agent":        true,
	}

	responseHeaders = map[string]bool{
		"cache-control":       true,
		"content-disposition": true,
		"content-encoding":    true,
		"content-language":    true,
		"content-type":        true,
		"etag":                true,
		"expires":             true,
		"last-modified":       true,
		"location":            true,
		"vary":                true,
	}

	forwardedIdentityHeaders = []string{
		"x-forwarded-email",
		"x-forwarded-groups",
		"x-forwarded-preferred-username",
		"x-forwarded-user",
	}

	validNameRegex    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$`)
	resourcePathRegex = regexp.MustCompile(`^/api/v1/(?:audits|clusters|jobs|peers|persistent-cache-tasks|scheduler-clusters|scheduler-features|schedulers|seed-peer-clusters|seed-peers)(?:/[A-Za-z0-9_-]+)?$`)
	staticPathRegex   = regexp.MustCompile(`^(?:/(?:static|fonts)/[A-Za-z0-9_./-]+|/(?:asset-manifest\.json|manifest\.json|favicon/favicon\.ico))$`)

	spaExact = map[string]bool{
		"/":         true,
		"/audit":    true,
		"/clusters": true,
		"/profile":  true,
		"/users":    true,
	}
	spaClustersRegex = regexp.MustCompile(`^/clusters/([A-Za-z0-9_-]+)(?:/(peers|schedulers(?:/[A-Za-z0-9_-]+)?))?$`)
	spaGCRegex       = regexp.MustCompile(`^/gc/[A-Za-z0-9_-]+$`)
	spaJobsPreheats  = regexp.MustCompile(`^/jobs/preheats(?:/([A-Za-z0-9_-]+))?$`)
	spaResourceCache = regexp.MustCompile(`^/resource/persistent-cache-task(?:/clusters/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?)?$`)
	spaResourceTask  = regexp.MustCompile(`^/resource/task/[A-Za-z0-9_-]+$`)
	spaResourceExec  = regexp.MustCompile(`^/resource/task/executions/[A-Za-z0-9_-]+$`)
)

type ManagerError struct {
	Message string
}

func (e *ManagerError) Error() string {
	return e.Message
}

type Identity struct {
	Issuer            string
	Subject           string
	PreferredUsername string
	Email             string
}

type identityKey struct {
	issuer  string
	subject string
}

type cacheEntry struct {
	userID            int64
	deadline          time.Time
	preferredUsername string
	email             string
}

type Config struct {
	ManagerHost     string
	ManagerPort     int
	CellID          string
	JWTKey          string
	OIDCIssuer      string
	OperatorGroup   string
	OperatorEmails  string
	TrustedProxies  string
	OAuthCookieName string
}

type Server struct {
	ManagerHost     string
	ManagerPort     int
	CellID          string
	JWTKey          string
	OIDCIssuer      string
	OperatorGroup   string
	OperatorEmails  string
	TrustedProxies  string
	OAuthCookieName string

	HTTPClient *http.Client
	nowFunc    func() time.Time

	mu            sync.Mutex
	identityCache map[identityKey]cacheEntry
}

func NewServer(cfg Config) *Server {
	if cfg.ManagerHost == "" {
		cfg.ManagerHost = defaultManagerHost
	}
	if cfg.ManagerPort == 0 {
		cfg.ManagerPort = defaultManagerPort
	}
	if cfg.OAuthCookieName == "" {
		cfg.OAuthCookieName = defaultOAuthCookieName
	}
	return &Server{
		ManagerHost:     cfg.ManagerHost,
		ManagerPort:     cfg.ManagerPort,
		CellID:          cfg.CellID,
		JWTKey:          cfg.JWTKey,
		OIDCIssuer:      cfg.OIDCIssuer,
		OperatorGroup:   cfg.OperatorGroup,
		OperatorEmails:  cfg.OperatorEmails,
		TrustedProxies:  cfg.TrustedProxies,
		OAuthCookieName: cfg.OAuthCookieName,
		HTTPClient:      &http.Client{Timeout: 30 * time.Second},
		identityCache:   make(map[identityKey]cacheEntry),
	}
}

func (s *Server) ClearIdentityCache() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.identityCache = make(map[identityKey]cacheEntry)
}

func (s *Server) now() time.Time {
	if s.nowFunc != nil {
		return s.nowFunc()
	}
	return time.Now()
}

func identityDigest(issuer, subject string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode([]string{issuer, subject})
	payload := bytes.TrimRight(buf.Bytes(), "\n")
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func mappedEmail(identity Identity) string {
	return fmt.Sprintf("%s@oidc.invalid", identityDigest(identity.Issuer, identity.Subject))
}

func profileMarker(email string) string {
	return fmt.Sprintf("oidc-profile-email:%s", email)
}

func subjectMarker(identity Identity) string {
	return fmt.Sprintf("oidc-subject-sha256:%s", identityDigest(identity.Issuer, identity.Subject))
}

func isTrustedProxy(clientIP string, trustedProxies string) bool {
	if clientIP == "" || clientIP == "127.0.0.1" || clientIP == "::1" || clientIP == "localhost" {
		return true
	}
	trustedEnv := strings.TrimSpace(trustedProxies)
	if trustedEnv == "" {
		return false
	}
	ip := net.ParseIP(clientIP)
	if ip == nil {
		return false
	}
	for _, entry := range strings.Split(trustedEnv, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		if strings.Contains(entry, "/") {
			_, cidrNet, err := net.ParseCIDR(entry)
			if err == nil && cidrNet.Contains(ip) {
				return true
			}
		} else {
			entryIP := net.ParseIP(entry)
			if entryIP != nil && entryIP.Equal(ip) {
				return true
			}
		}
	}
	return false
}

func verifyForwardedHeaders(headers http.Header, key string) bool {
	if key == "" {
		return false
	}
	sig := headers.Get("x-forwarded-signature")
	if sig == "" {
		sig = headers.Get("x-signature")
	}
	if sig == "" {
		return false
	}

	parts := make([]string, 0, len(forwardedIdentityHeaders))
	for _, name := range forwardedIdentityHeaders {
		parts = append(parts, headers.Get(name))
	}
	data := strings.Join(parts, ":")

	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(data))
	expected := hex.EncodeToString(mac.Sum(nil))

	return subtle.ConstantTimeCompare([]byte(strings.ToLower(sig)), []byte(expected)) == 1
}

func isTrustedForwardRequest(headers http.Header, clientIP string, signingKey string, trustedProxies string) bool {
	hasIdentityHeaders := false
	for _, name := range forwardedIdentityHeaders {
		if headers.Get(name) != "" {
			hasIdentityHeaders = true
			break
		}
	}
	if !hasIdentityHeaders {
		return true
	}
	if isTrustedProxy(clientIP, trustedProxies) {
		return true
	}
	return verifyForwardedHeaders(headers, signingKey)
}

func assertedIdentity(headers http.Header, issuer, operatorGroup, operatorEmails string, trusted bool) (Identity, error) {
	if !trusted {
		return Identity{}, errors.New("untrusted forward headers")
	}
	subject := headers.Get("x-forwarded-user")
	email := headers.Get("x-forwarded-email")
	preferredUsername := headers.Get("x-forwarded-preferred-username")
	if preferredUsername == "" && strings.Contains(email, "@") {
		preferredUsername = strings.SplitN(email, "@", 2)[0]
	}

	rawGroups := headers.Get("x-forwarded-groups")
	var groups []string
	if rawGroups != "" {
		decoded, err := base64.StdEncoding.Strict().DecodeString(rawGroups)
		if err != nil {
			return Identity{}, errors.New("invalid Dragonfly operator groups")
		}
		var rawList []json.RawMessage
		if err := json.Unmarshal(decoded, &rawList); err != nil {
			return Identity{}, errors.New("invalid Dragonfly operator groups")
		}
		groups = make([]string, len(rawList))
		for i, rawElem := range rawList {
			if err := json.Unmarshal(rawElem, &groups[i]); err != nil {
				return Identity{}, errors.New("invalid Dragonfly operator groups")
			}
		}
	}

	validName := validNameRegex.MatchString(preferredUsername)

	allowedGroups := make(map[string]bool)
	for _, g := range strings.Split(operatorGroup, ",") {
		g = strings.TrimSpace(g)
		if g != "" {
			allowedGroups[g] = true
		}
	}

	matchesGroup := false
	for _, g := range groups {
		for ag := range allowedGroups {
			if g == ag || strings.HasPrefix(g, ag+"@") {
				matchesGroup = true
				break
			}
		}
		if matchesGroup {
			break
		}
	}

	allowedEmails := make(map[string]bool)
	for _, e := range strings.Split(operatorEmails, ",") {
		e = strings.TrimSpace(e)
		if e != "" {
			allowedEmails[strings.ToLower(e)] = true
		}
	}
	matchesEmail := allowedEmails[strings.ToLower(email)]

	validUser := validName && strings.Contains(email, "@") && (matchesGroup || matchesEmail)
	if issuer == "" || subject == "" || email == "" || !validUser {
		return Identity{}, errors.New("missing required Dragonfly operator identity")
	}

	return Identity{
		Issuer:            issuer,
		Subject:           subject,
		PreferredUsername: preferredUsername,
		Email:             email,
	}, nil
}

type jwtClaims struct {
	Cell    string `json:"cell"`
	Exp     int64  `json:"exp"`
	ID      int64  `json:"id"`
	OrigIat int64  `json:"orig_iat"`
}

func userToken(userID int64, key, cell string, now ...int64) string {
	var issuedAt int64
	if len(now) > 0 {
		issuedAt = now[0]
	} else {
		issuedAt = time.Now().Unix()
	}

	headerJSON := `{"alg":"HS256","typ":"JWT"}`
	headerEnc := base64.RawURLEncoding.EncodeToString([]byte(headerJSON))

	claims := jwtClaims{
		Cell:    cell,
		Exp:     issuedAt + sessionSeconds,
		ID:      userID,
		OrigIat: issuedAt,
	}
	claimsBytes, _ := json.Marshal(claims)
	claimsEnc := base64.RawURLEncoding.EncodeToString(claimsBytes)

	unsigned := headerEnc + "." + claimsEnc
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(unsigned))
	signatureEnc := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))

	return unsigned + "." + signatureEnc
}

func administrativeToken(key, cell string) string {
	return userToken(rootUserID, key, cell)
}

type Target struct {
	Path     string
	RawQuery string
}

func canonicalTarget(rawTarget string) (*Target, bool) {
	if !strings.HasPrefix(rawTarget, "/") || strings.HasPrefix(rawTarget, "//") {
		return nil, false
	}
	if strings.ContainsAny(rawTarget, "\\\r\n\t#") {
		return nil, false
	}

	path := rawTarget
	rawQuery := ""
	if idx := strings.IndexByte(rawTarget, '?'); idx != -1 {
		path = rawTarget[:idx]
		rawQuery = rawTarget[idx+1:]
	}

	if strings.Contains(path, "%") || strings.Contains(rawQuery, ";") {
		return nil, false
	}

	if path != "/" {
		segments := strings.Split(path, "/")[1:]
		for _, seg := range segments {
			if seg == "" || seg == "." || seg == ".." {
				return nil, false
			}
		}
	}

	return &Target{
		Path:     path,
		RawQuery: rawQuery,
	}, true
}

func requestHeadersSafe(headers http.Header) bool {
	if headers == nil {
		return true
	}
	if headers.Get("Authorization") != "" {
		return false
	}
	credentialNames := map[string]bool{
		"access_token":  true,
		"authorization": true,
		"token":         true,
	}
	for _, cookieHeader := range headers.Values("Cookie") {
		for _, part := range strings.Split(cookieHeader, ";") {
			if idx := strings.IndexByte(part, '='); idx != -1 {
				name := strings.ToLower(strings.TrimSpace(part[:idx]))
				if credentialNames[name] {
					return false
				}
			}
		}
	}
	return true
}

func isSPAPath(p string) bool {
	if spaExact[p] {
		return true
	}
	if m := spaClustersRegex.FindStringSubmatch(p); m != nil {
		return m[1] != "new"
	}
	if spaGCRegex.MatchString(p) {
		return true
	}
	if m := spaJobsPreheats.FindStringSubmatch(p); m != nil {
		if len(m) > 1 && m[1] == "new" {
			return false
		}
		return true
	}
	if spaResourceCache.MatchString(p) {
		return true
	}
	if spaResourceTask.MatchString(p) {
		return true
	}
	if spaResourceExec.MatchString(p) {
		return true
	}
	return false
}

func allowedRequest(method, rawTarget string, userID int64, headers http.Header) bool {
	target, ok := canonicalTarget(rawTarget)
	if !ok || !requestHeadersSafe(headers) || method != http.MethodGet {
		return false
	}
	if target.RawQuery != "" {
		for _, param := range strings.Split(target.RawQuery, "&") {
			if param == "" {
				continue
			}
			key := param
			if idx := strings.IndexByte(param, '='); idx != -1 {
				key = param[:idx]
			}
			if unescaped, err := url.QueryUnescape(key); err == nil {
				key = unescaped
			}
			k := strings.ToLower(key)
			if k == "access_token" || k == "token" {
				return false
			}
		}
	}
	if target.Path == "/api/v1/users" ||
		target.Path == fmt.Sprintf("/api/v1/users/%d", userID) ||
		target.Path == fmt.Sprintf("/api/v1/users/%d/roles", userID) {
		return true
	}
	return resourcePathRegex.MatchString(target.Path) ||
		isSPAPath(target.Path) ||
		staticPathRegex.MatchString(target.Path)
}

func managerTarget(rawTarget string) string {
	target, ok := canonicalTarget(rawTarget)
	if ok && isSPAPath(target.Path) {
		return "/"
	}
	return rawTarget
}

func checkResponseStatus(status int) error {
	if status < 200 || status >= 300 {
		return &ManagerError{Message: fmt.Sprintf("Dragonfly Manager returned HTTP %d", status)}
	}
	return nil
}

func (s *Server) requestManager(ctx context.Context, method, path string, body any, token string) (int, []byte, error) {
	var bodyReader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		bodyReader = bytes.NewReader(b)
	}
	targetURL := fmt.Sprintf("http://%s:%d%s", s.ManagerHost, s.ManagerPort, path)
	req, err := http.NewRequestWithContext(ctx, method, targetURL, bodyReader)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := s.HTTPClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, err
	}
	return resp.StatusCode, data, nil
}

func rolesMatch(actual map[permissionPair]bool) bool {
	if len(actual) != len(expectedPermissions) {
		return false
	}
	for _, ep := range expectedPermissions {
		if !actual[ep] {
			return false
		}
	}
	return true
}

func (s *Server) ensureRole(ctx context.Context, token string) error {
	status, payload, err := s.requestManager(ctx, http.MethodGet, fmt.Sprintf("/api/v1/roles/%s", ssoRole), nil, token)
	if err != nil {
		return err
	}
	if err := checkResponseStatus(status); err != nil {
		return err
	}
	var permissions [][]string
	if err := json.Unmarshal(payload, &permissions); err != nil {
		return &ManagerError{Message: "Dragonfly SSO role permissions have drifted"}
	}
	actual := make(map[permissionPair]bool)
	for _, entry := range permissions {
		if len(entry) == 3 && entry[0] == ssoRole {
			actual[permissionPair{resource: entry[1], action: entry[2]}] = true
		}
	}
	if len(permissions) != 0 && (!rolesMatch(actual) || len(actual) != len(permissions)) {
		status, _, err := s.requestManager(ctx, http.MethodDelete, fmt.Sprintf("/api/v1/roles/%s", ssoRole), nil, token)
		if err != nil {
			return err
		}
		if status != http.StatusOK && status != http.StatusNoContent {
			return &ManagerError{Message: fmt.Sprintf("Dragonfly role recreation returned HTTP %d", status)}
		}
		permissions = nil
	}
	if len(permissions) == 0 {
		type permReq struct {
			Action string `json:"action"`
			Object string `json:"object"`
		}
		type createRoleReq struct {
			Permissions []permReq `json:"permissions"`
			Role        string    `json:"role"`
		}
		perms := make([]permReq, 0, len(expectedPermissions))
		for _, ep := range expectedPermissions {
			perms = append(perms, permReq{Action: ep.action, Object: ep.resource})
		}
		reqBody := createRoleReq{
			Permissions: perms,
			Role:        ssoRole,
		}
		status, _, err := s.requestManager(ctx, http.MethodPost, "/api/v1/roles", reqBody, token)
		if err != nil {
			return err
		}
		if status != http.StatusOK && status != http.StatusCreated {
			return &ManagerError{Message: fmt.Sprintf("Dragonfly role creation returned HTTP %d", status)}
		}
		status, payload, err = s.requestManager(ctx, http.MethodGet, fmt.Sprintf("/api/v1/roles/%s", ssoRole), nil, token)
		if err != nil {
			return err
		}
		if err := checkResponseStatus(status); err != nil {
			return err
		}
		if err := json.Unmarshal(payload, &permissions); err != nil {
			return &ManagerError{Message: "Dragonfly SSO role permissions have drifted"}
		}
	}
	actual = make(map[permissionPair]bool)
	for _, entry := range permissions {
		if len(entry) == 3 && entry[0] == ssoRole {
			actual[permissionPair{resource: entry[1], action: entry[2]}] = true
		}
	}
	if !rolesMatch(actual) || len(actual) != len(permissions) {
		return &ManagerError{Message: "Dragonfly SSO role permissions have drifted"}
	}
	return nil
}

type ManagerUser struct {
	ID    int64  `json:"id"`
	Name  string `json:"name"`
	Email string `json:"email"`
	Bio   string `json:"bio"`
	State string `json:"state"`
}

func (s *Server) findUser(ctx context.Context, identity Identity, token string) (*ManagerUser, error) {
	status, payload, err := s.requestManager(ctx, http.MethodGet, "/api/v1/users?page=1&per_page=10000000", nil, token)
	if err != nil {
		return nil, err
	}
	if err := checkResponseStatus(status); err != nil {
		return nil, err
	}
	var users []ManagerUser
	if err := json.Unmarshal(payload, &users); err != nil {
		return nil, &ManagerError{Message: "Dragonfly identity lookup was ambiguous"}
	}

	legacyEmail := mappedEmail(identity)
	marker := subjectMarker(identity)
	profMarker := profileMarker(identity.Email)

	matchedIndices := make(map[int]bool)
	var matches []ManagerUser
	for i, u := range users {
		if u.Bio == marker || u.Email == legacyEmail {
			matches = append(matches, u)
			matchedIndices[i] = true
		}
	}

	var conflicts []ManagerUser
	for i, u := range users {
		if !matchedIndices[i] {
			if u.Name == identity.PreferredUsername ||
				u.Email == identity.Email ||
				u.Bio == marker ||
				u.Bio == profMarker {
				conflicts = append(conflicts, u)
			}
		}
	}

	if len(matches) > 1 || len(conflicts) > 0 {
		return nil, &ManagerError{Message: "Dragonfly profile is bound to another OIDC identity"}
	}
	if len(matches) == 1 {
		return &matches[0], nil
	}
	return nil, nil
}

func randomPassword() string {
	b := make([]byte, 15)
	_, _ = rand.Read(b)
	s := base64.RawURLEncoding.EncodeToString(b)
	if len(s) > 20 {
		return s[:20]
	}
	return s
}

func (s *Server) createUser(ctx context.Context, identity Identity, token string) (*ManagerUser, error) {
	expectedBio := subjectMarker(identity)
	reqBody := map[string]string{
		"bio":      expectedBio,
		"email":    identity.Email,
		"name":     identity.PreferredUsername,
		"password": randomPassword(),
	}
	status, _, err := s.requestManager(ctx, http.MethodPost, "/api/v1/users/signup", reqBody, "")
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK && status != http.StatusCreated {
		return nil, &ManagerError{Message: fmt.Sprintf("Dragonfly user creation returned HTTP %d", status)}
	}
	user, err := s.findUser(ctx, identity, token)
	if err != nil {
		return nil, err
	}
	if user == nil {
		return nil, &ManagerError{Message: "Dragonfly identity mapping did not create a user"}
	}
	return user, nil
}

func (s *Server) validateAndSyncProfile(ctx context.Context, user *ManagerUser, identity Identity, token string) (int64, error) {
	if user.State != "enable" {
		return 0, &ManagerError{Message: "Dragonfly identity mapping conflicts with an existing user"}
	}
	if user.ID < 1 {
		return 0, &ManagerError{Message: "Dragonfly identity mapping returned an invalid user ID"}
	}
	expectedBio := subjectMarker(identity)
	if user.Bio != expectedBio || user.Email != identity.Email || user.Name != identity.PreferredUsername {
		profile := map[string]string{
			"bio":   expectedBio,
			"email": identity.Email,
			"name":  identity.PreferredUsername,
		}
		status, _, err := s.requestManager(ctx, http.MethodPatch, fmt.Sprintf("/api/v1/users/%d", user.ID), profile, token)
		if err != nil {
			return 0, err
		}
		if status != http.StatusOK && status != http.StatusNoContent {
			return 0, &ManagerError{Message: fmt.Sprintf("Dragonfly profile update returned HTTP %d", status)}
		}
	}
	return user.ID, nil
}

func (s *Server) syncUserRoles(ctx context.Context, userID int64, token string) error {
	status, payload, err := s.requestManager(ctx, http.MethodGet, fmt.Sprintf("/api/v1/users/%d/roles", userID), nil, token)
	if err != nil {
		return err
	}
	if err := checkResponseStatus(status); err != nil {
		return err
	}
	var roles []string
	if err := json.Unmarshal(payload, &roles); err != nil {
		return &ManagerError{Message: "Dragonfly returned an invalid role set"}
	}
	hasSSORole := false
	for _, role := range roles {
		if role != ssoRole {
			status, _, err := s.requestManager(ctx, http.MethodDelete, fmt.Sprintf("/api/v1/users/%d/roles/%s", userID, url.PathEscape(role)), nil, token)
			if err != nil {
				return err
			}
			if status != http.StatusOK && status != http.StatusNoContent {
				return &ManagerError{Message: fmt.Sprintf("Dragonfly role removal returned HTTP %d", status)}
			}
		} else {
			hasSSORole = true
		}
	}
	if !hasSSORole {
		status, _, err := s.requestManager(ctx, http.MethodPut, fmt.Sprintf("/api/v1/users/%d/roles/%s", userID, ssoRole), nil, token)
		if err != nil {
			return err
		}
		if status != http.StatusOK && status != http.StatusNoContent {
			return &ManagerError{Message: fmt.Sprintf("Dragonfly role assignment returned HTTP %d", status)}
		}
	}
	status, payload, err = s.requestManager(ctx, http.MethodGet, fmt.Sprintf("/api/v1/users/%d/roles", userID), nil, token)
	if err != nil {
		return err
	}
	if err := checkResponseStatus(status); err != nil {
		return err
	}
	var finalRoles []string
	if err := json.Unmarshal(payload, &finalRoles); err != nil {
		return &ManagerError{Message: "Dragonfly SSO user does not have the exact required role"}
	}
	if len(finalRoles) != 1 || finalRoles[0] != ssoRole {
		return &ManagerError{Message: "Dragonfly SSO user does not have the exact required role"}
	}
	return nil
}

func (s *Server) ensureUser(ctx context.Context, identity Identity) (int64, error) {
	token := administrativeToken(s.JWTKey, s.CellID)
	if err := s.ensureRole(ctx, token); err != nil {
		return 0, err
	}
	user, err := s.findUser(ctx, identity, token)
	if err != nil {
		return 0, err
	}
	if user == nil {
		user, err = s.createUser(ctx, identity, token)
		if err != nil {
			return 0, err
		}
	}
	userID, err := s.validateAndSyncProfile(ctx, user, identity, token)
	if err != nil {
		return 0, err
	}
	if err := s.syncUserRoles(ctx, userID, token); err != nil {
		return 0, err
	}
	return userID, nil
}

func (s *Server) resolveUser(ctx context.Context, identity Identity) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.now()
	key := identityKey{issuer: identity.Issuer, subject: identity.Subject}
	if cached, ok := s.identityCache[key]; ok {
		if now.Before(cached.deadline) && cached.preferredUsername == identity.PreferredUsername && cached.email == identity.Email {
			return cached.userID, nil
		}
	}

	for k, v := range s.identityCache {
		if !now.Before(v.deadline) {
			delete(s.identityCache, k)
		}
	}

	userID, err := s.ensureUser(ctx, identity)
	if err != nil {
		return 0, err
	}

	s.identityCache[key] = cacheEntry{
		userID:            userID,
		deadline:          now.Add(sessionDuration),
		preferredUsername: identity.PreferredUsername,
		email:             identity.Email,
	}
	return userID, nil
}

func (s *Server) handleSignOut(w http.ResponseWriter) {
	w.Header().Add("Set-Cookie", "jwt=; Max-Age=0; Path=/; Secure; SameSite=Lax")
	cookieName := s.OAuthCookieName
	if cookieName == "" {
		cookieName = defaultOAuthCookieName
	}
	w.Header().Add("Set-Cookie", fmt.Sprintf("%s=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax", cookieName))
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(http.StatusOK)
}

func (s *Server) proxy(w http.ResponseWriter, r *http.Request, token, rawTarget string) {
	targetURL := fmt.Sprintf("http://%s:%d%s", s.ManagerHost, s.ManagerPort, managerTarget(rawTarget))
	outReq, err := http.NewRequestWithContext(r.Context(), http.MethodGet, targetURL, nil)
	if err != nil {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}
	for name, values := range r.Header {
		if forwardedHeaders[strings.ToLower(name)] {
			for _, v := range values {
				outReq.Header.Add(name, v)
			}
		}
	}
	outReq.Header.Set("Authorization", "Bearer "+token)

	resp, err := s.HTTPClient.Do(outReq)
	if err != nil {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}

	for name, values := range resp.Header {
		if responseHeaders[strings.ToLower(name)] {
			for _, v := range values {
				w.Header().Add(name, v)
			}
		}
	}
	w.Header().Add("Set-Cookie", fmt.Sprintf("jwt=%s; Max-Age=%d; Path=/; Secure; SameSite=Lax", token, sessionSeconds))
	w.Header().Set("Content-Length", strconv.Itoa(len(payload)))
	w.WriteHeader(resp.StatusCode)
	w.Write(payload)
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	rawTarget := r.RequestURI
	if rawTarget == "" && r.URL != nil {
		rawTarget = r.URL.RequestURI()
	}

	targetPath := r.URL.Path
	if r.Method == http.MethodPost && targetPath == "/api/v1/users/signout" {
		s.handleSignOut(w)
		return
	}

	clientIP := r.RemoteAddr
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		clientIP = host
	}

	if !isTrustedForwardRequest(r.Header, clientIP, s.JWTKey, s.TrustedProxies) {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	if s.OIDCIssuer == "" || s.OperatorGroup == "" || s.JWTKey == "" || s.CellID == "" {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}

	identity, err := assertedIdentity(r.Header, s.OIDCIssuer, s.OperatorGroup, s.OperatorEmails, true)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	userID, err := s.resolveUser(r.Context(), identity)
	if err != nil {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}

	if !allowedRequest(r.Method, rawTarget, userID, r.Header) {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}

	token := userToken(userID, s.JWTKey, s.CellID)
	s.proxy(w, r, token, rawTarget)
}

func loadConfigFromEnv() Config {
	port := defaultManagerPort
	if p := os.Getenv("MANAGER_PORT"); p != "" {
		if val, err := strconv.Atoi(p); err == nil {
			port = val
		}
	}
	mgrHost := os.Getenv("MANAGER_HOST")
	if mgrHost == "" {
		mgrHost = defaultManagerHost
	}
	cookieName := os.Getenv("OAUTH_COOKIE_NAME")
	if cookieName == "" {
		cookieName = defaultOAuthCookieName
	}
	return Config{
		ManagerHost:     mgrHost,
		ManagerPort:     port,
		CellID:          os.Getenv("CELL_ID"),
		JWTKey:          os.Getenv("JWT_KEY"),
		OIDCIssuer:      os.Getenv("OIDC_ISSUER"),
		OperatorGroup:   os.Getenv("OPERATOR_GROUP"),
		OperatorEmails:  os.Getenv("OPERATOR_EMAILS"),
		TrustedProxies:  os.Getenv("TRUSTED_PROXIES"),
		OAuthCookieName: cookieName,
	}
}

func main() {
	cfg := loadConfigFromEnv()
	server := NewServer(cfg)
	log.Printf("Starting Dragonfly SSO bridge on :8081")
	if err := http.ListenAndServe(":8081", server); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}
