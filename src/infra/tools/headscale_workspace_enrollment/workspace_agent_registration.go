// Binds provisioner tokens to completed Coder build records to authenticate workspace agents.

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	registrationAudience        = "workspace-agent-registration"
	registrationPath            = "/v1/workspace-agents/register"
	registrationFinalizeTimeout = 15 * time.Minute
	registrationPendingLimit    = 128
	registrationPollInterval    = 500 * time.Millisecond
	registrationRateLimit       = 120
	registrationRateWindow      = time.Minute
	registrationServiceAccount  = "system:serviceaccount:coder:coder"
	workspaceAgentBindingLimit  = 10000
	workspaceAgentFileLimit     = 4 << 20
)

const workspaceAgentTokenHashDomain = "coder-agent-binding\x00"

var errRegistrationPending = errors.New("Coder build has not completed")

type provisionerTokenReviewer interface {
	authenticate(context.Context, string, string, string) error
}

type workspaceReader interface {
	workspace(context.Context, string, string) (coderWorkspace, error)
}

type workspaceAgentRegistrationRequest struct {
	AgentToken      string `json:"agentToken"`
	BuildID         string `json:"buildId"`
	Cell            string `json:"cell"`
	Incarnation     string `json:"incarnation"`
	IsPrebuildClaim *bool  `json:"isPrebuildClaim"`
	Lineage         string `json:"lineage"`
	ParentLineage   string `json:"parentLineage,omitempty"`
	ParentSnapshot  string `json:"parentSnapshot,omitempty"`
	IsRoot          *bool  `json:"isRoot,omitempty"`
	Machine         string `json:"machine"`
	OwnerID         string `json:"ownerId"`
	Team            string `json:"team"`
	WorkspaceID     string `json:"workspaceId"`
	WorkspaceName   string `json:"workspaceName"`
	WorkspaceVolume string `json:"workspaceVolume"`
}

type workspaceAgentMetadata struct {
	BuildID         string `json:"buildId"`
	Cell            string `json:"cell"`
	Incarnation     string `json:"incarnation"`
	Lineage         string `json:"lineage"`
	ParentLineage   string `json:"parentLineage,omitempty"`
	ParentSnapshot  string `json:"parentSnapshot,omitempty"`
	IsRoot          *bool  `json:"isRoot,omitempty"`
	Machine         string `json:"machine"`
	OwnerID         string `json:"ownerId"`
	Team            string `json:"team,omitempty"`
	WorkspaceID     string `json:"workspaceId"`
	WorkspaceName   string `json:"workspaceName"`
	WorkspaceVolume string `json:"workspaceVolume"`
}

type workspaceAgentBinding struct {
	AgentID   string `json:"agentId"`
	TokenHash string `json:"tokenHash"`
	workspaceAgentMetadata
}

type workspaceAgentBindingFile struct {
	Bindings []workspaceAgentBinding `json:"bindings"`
	Schema   int                     `json:"schema"`
}

type pendingWorkspaceAgent struct {
	cancel       context.CancelFunc
	metadata     workspaceAgentMetadata
	ownerSession []byte
	tokenHash    string
}

type workspaceAgentRegistry struct {
	cache           map[string]workspaceAgentBinding
	cacheFile       os.FileInfo
	finalizeTimeout time.Duration
	mu              *sync.Mutex
	path            string
	pending         map[string]*pendingWorkspaceAgent
	pendingMu       *sync.Mutex
	pollInterval    time.Duration
	reader          workspaceReader
	validator       agentTokenValidator
}

type registrationRateLimiter struct {
	count       int
	mu          sync.Mutex
	now         func() time.Time
	windowStart time.Time
}

type kubernetesTokenReviewer struct {
	client         *http.Client
	credentialPath string
	endpoint       string
}

func (s server) authenticateProvisioner(r *http.Request) (status int, err error) {
	if s.registrationLimiter != nil && !s.registrationLimiter.allow() {
		return http.StatusTooManyRequests, errors.New("registration rate limit exceeded")
	}
	provisionerToken := bearerToken(r.Header.Get("Authorization"))
	if provisionerToken == "" || s.provisionerAuth == nil ||
		s.provisionerAuth.authenticate(r.Context(), provisionerToken, registrationAudience, registrationServiceAccount) != nil {
		return http.StatusUnauthorized, errors.New("unauthorized")
	}
	return http.StatusOK, nil
}

func (s server) verifyOwnerIdentity(ctx context.Context, ownerSession string, request workspaceAgentRegistrationRequest) (status int, err error) {
	if s.ownerAuth == nil || s.agentRegistry == nil || s.agentRegistry.reader == nil {
		return http.StatusServiceUnavailable, errors.New("workspace agent registration unavailable")
	}
	owner, err := s.ownerAuth.authenticateOwner(ctx, ownerSession)
	if err != nil || !ownerMatchesRegistration(owner, request) {
		return http.StatusUnauthorized, errors.New("unauthorized")
	}
	bound, err := s.bindingForOwner(owner.ID)
	if err != nil || owner.Issuer != bound.Issuer || owner.Subject != bound.Subject ||
		owner.PreferredUsername != bound.PreferredUsername {
		return http.StatusConflict, errors.New("owner identity binding conflict")
	}
	return http.StatusOK, nil
}

func (s server) registerWorkspaceAgent(w http.ResponseWriter, r *http.Request) {
	if status, err := s.authenticateProvisioner(r); err != nil {
		http.Error(w, err.Error(), status)
		return
	}
	var request workspaceAgentRegistrationRequest
	if decodeStrictJSON(r.Body, &request) != nil || validateWorkspaceAgentRegistration(request) != nil {
		http.Error(w, "invalid workspace agent registration", http.StatusBadRequest)
		return
	}
	ownerSession := r.Header.Get("Coder-Session-Token")
	if status, err := s.verifyOwnerIdentity(r.Context(), ownerSession, request); err != nil {
		http.Error(w, err.Error(), status)
		return
	}
	workspace, err := s.agentRegistry.reader.workspace(r.Context(), ownerSession, request.WorkspaceID)
	if err != nil || validateRegistrationWorkspace(request.metadata(), workspace, false) != nil {
		http.Error(w, "workspace build does not match registration", http.StatusConflict)
		return
	}
	status, err := s.agentRegistry.begin(request, []byte(ownerSession))
	if err != nil {
		http.Error(w, "workspace agent registration conflict", http.StatusConflict)
		return
	}
	w.WriteHeader(status)
}

func (r workspaceAgentRegistrationRequest) metadata() workspaceAgentMetadata {
	isRoot := true
	if r.IsRoot != nil {
		isRoot = *r.IsRoot
	} else if r.ParentLineage != "" || r.ParentSnapshot != "" {
		isRoot = false
	}
	return workspaceAgentMetadata{
		BuildID: r.BuildID, Cell: r.Cell, Incarnation: r.Incarnation,
		Lineage: r.Lineage, ParentLineage: r.ParentLineage,
		ParentSnapshot: r.ParentSnapshot, IsRoot: &isRoot,
		Machine: r.Machine, OwnerID: r.OwnerID, Team: r.Team,
		WorkspaceID: r.WorkspaceID, WorkspaceName: r.WorkspaceName, WorkspaceVolume: r.WorkspaceVolume,
	}
}

func validateWorkspaceAgentRegistration(request workspaceAgentRegistrationRequest) error {
	metadata := request.metadata()
	if !userID.MatchString(request.AgentToken) || request.IsPrebuildClaim == nil || *request.IsPrebuildClaim ||
		validateWorkspaceAgentMetadata(metadata) != nil {
		return errors.New("invalid workspace agent registration")
	}
	return nil
}

func validWorkspaceAgentIdentifiers(metadata workspaceAgentMetadata) bool {
	return userID.MatchString(metadata.BuildID) && userID.MatchString(metadata.OwnerID) &&
		userID.MatchString(metadata.WorkspaceID) && label.MatchString(metadata.Cell) &&
		incarnationName.MatchString(metadata.Incarnation) && lineageName.MatchString(metadata.Lineage) &&
		label.MatchString(metadata.Machine) && (metadata.Team == "" || label.MatchString(metadata.Team)) &&
		label.MatchString(metadata.WorkspaceName) && metadata.WorkspaceVolume == "/var/lib/workspace"
}

func validWorkspaceAgentAncestry(metadata workspaceAgentMetadata) error {
	isRoot := metadata.IsRoot == nil || *metadata.IsRoot
	if isRoot {
		if metadata.ParentLineage != "" || metadata.ParentSnapshot != "" {
			return errors.New("root workspace agent metadata must not specify parent ancestry")
		}
		return nil
	}
	if !lineageName.MatchString(metadata.ParentLineage) || !snapshotSelector.MatchString(metadata.ParentSnapshot) {
		return errors.New("non-root workspace agent metadata must specify valid parent lineage and snapshot")
	}
	return nil
}

func validateWorkspaceAgentMetadata(metadata workspaceAgentMetadata) error {
	if !validWorkspaceAgentIdentifiers(metadata) {
		return errors.New("invalid workspace agent metadata")
	}
	return validWorkspaceAgentAncestry(metadata)
}

func equalWorkspaceAgentMetadata(a, b workspaceAgentMetadata) bool {
	aRoot := a.IsRoot == nil || *a.IsRoot
	bRoot := b.IsRoot == nil || *b.IsRoot
	return a.BuildID == b.BuildID &&
		a.Cell == b.Cell &&
		a.Incarnation == b.Incarnation &&
		a.Lineage == b.Lineage &&
		a.ParentLineage == b.ParentLineage &&
		a.ParentSnapshot == b.ParentSnapshot &&
		aRoot == bRoot &&
		a.Machine == b.Machine &&
		a.OwnerID == b.OwnerID &&
		a.Team == b.Team &&
		a.WorkspaceID == b.WorkspaceID &&
		a.WorkspaceName == b.WorkspaceName &&
		a.WorkspaceVolume == b.WorkspaceVolume
}

func equalWorkspaceAgentBinding(a, b workspaceAgentBinding) bool {
	return a.AgentID == b.AgentID &&
		a.TokenHash == b.TokenHash &&
		equalWorkspaceAgentMetadata(a.workspaceAgentMetadata, b.workspaceAgentMetadata)
}

func ownerMatchesRegistration(owner coderUser, request workspaceAgentRegistrationRequest) bool {
	if owner.ID != request.OwnerID {
		return false
	}
	if request.Team == "" {
		return true
	}
	wantedGroup := "team:" + request.Team
	for _, group := range owner.Groups {
		if group == wantedGroup {
			return true
		}
	}
	return false
}

func workspaceMatchesRegistration(metadata workspaceAgentMetadata, workspace coderWorkspace) bool {
	build := workspace.LatestBuild
	return !workspace.IsPrebuild && workspace.ID == metadata.WorkspaceID && workspace.OwnerID == metadata.OwnerID &&
		workspace.Name == metadata.WorkspaceName && build.ID == metadata.BuildID &&
		build.WorkspaceID == metadata.WorkspaceID && build.WorkspaceOwnerID == metadata.OwnerID &&
		build.Transition == "start"
}

func validateActiveBuild(build coderWorkspaceBuild) error {
	switch build.Status {
	case "pending", "starting":
		if build.Job.Status != "pending" && build.Job.Status != "running" {
			return errors.New("Coder workspace build job is not active")
		}
		return nil
	case "running":
		if build.Job.Status != "succeeded" {
			return errors.New("Coder workspace build did not succeed")
		}
		return nil
	default:
		return errors.New("Coder workspace build is not active")
	}
}

func validateCompletedBuild(build coderWorkspaceBuild) error {
	if build.Status == "running" && build.Job.Status == "succeeded" {
		return nil
	}
	switch build.Status {
	case "pending", "starting":
		return errRegistrationPending
	default:
		return errors.New("Coder workspace build did not succeed")
	}
}

func validateRegistrationWorkspace(metadata workspaceAgentMetadata, workspace coderWorkspace, completed bool) error {
	if !workspaceMatchesRegistration(metadata, workspace) {
		return errors.New("Coder workspace does not match registration")
	}
	if !completed {
		return validateActiveBuild(workspace.LatestBuild)
	}
	return validateCompletedBuild(workspace.LatestBuild)
}

func registrationAgent(workspace coderWorkspace) (string, error) {
	var match string
	for _, resource := range workspace.LatestBuild.Resources {
		for _, agent := range resource.Agents {
			if agent.Name != "main" || agent.ParentID != nil {
				return "", errors.New("Coder build contains an unexpected workspace agent")
			}
			if match != "" || !userID.MatchString(agent.ID) {
				return "", errors.New("Coder build must contain exactly one main agent")
			}
			match = agent.ID
		}
	}
	if match == "" {
		return "", errors.New("Coder build must contain exactly one main agent")
	}
	return match, nil
}

func matchExistingAgentBinding(bindings []workspaceAgentBinding, metadata workspaceAgentMetadata, tokenHash string) (bool, error) {
	for _, existing := range bindings {
		if existing.WorkspaceID == metadata.WorkspaceID && existing.BuildID == metadata.BuildID &&
			existing.TokenHash == tokenHash && equalWorkspaceAgentMetadata(existing.workspaceAgentMetadata, metadata) {
			return true, nil
		}
		if existing.TokenHash == tokenHash {
			return false, errors.New("workspace agent token is already bound")
		}
	}
	return false, nil
}

func (r *workspaceAgentRegistry) checkPendingConflicts(metadata workspaceAgentMetadata, tokenHash string) (bool, error) {
	for workspaceID, active := range r.pending {
		if active.tokenHash == tokenHash && workspaceID != metadata.WorkspaceID {
			return false, errors.New("workspace agent token registration is already pending")
		}
	}
	if current := r.pending[metadata.WorkspaceID]; current != nil {
		if equalWorkspaceAgentMetadata(current.metadata, metadata) && current.tokenHash == tokenHash {
			return true, nil
		}
		if current.metadata.BuildID == metadata.BuildID {
			return false, errors.New("conflicting registration for active build")
		}
		current.cancel()
	} else if len(r.pending) >= registrationPendingLimit {
		return false, errors.New("workspace agent registration limit exceeded")
	}
	return false, nil
}

func (r *workspaceAgentRegistry) begin(request workspaceAgentRegistrationRequest, ownerSession []byte) (int, error) {
	metadata := request.metadata()
	tokenHash := workspaceAgentTokenHash(request.AgentToken)
	context, cancel := context.WithCancel(context.Background())
	pending := &pendingWorkspaceAgent{
		cancel: cancel, metadata: metadata, ownerSession: ownerSession, tokenHash: tokenHash,
	}
	cleanup := func() {
		clear(ownerSession)
		cancel()
	}
	r.pendingMu.Lock()
	defer r.pendingMu.Unlock()
	r.mu.Lock()
	bindings, err := readWorkspaceAgentBindings(r.path)
	r.mu.Unlock()
	if err != nil {
		cleanup()
		return 0, err
	}
	alreadyBound, err := matchExistingAgentBinding(bindings, metadata, tokenHash)
	if err != nil {
		cleanup()
		return 0, err
	}
	if alreadyBound {
		cleanup()
		return http.StatusAccepted, nil
	}
	alreadyPending, err := r.checkPendingConflicts(metadata, tokenHash)
	if err != nil {
		cleanup()
		return 0, err
	}
	if alreadyPending {
		cleanup()
		return http.StatusAccepted, nil
	}
	r.pending[metadata.WorkspaceID] = pending
	go r.finalize(context, pending)
	return http.StatusAccepted, nil
}

func (r *workspaceAgentRegistry) finalize(parent context.Context, pending *pendingWorkspaceAgent) {
	timeout := r.finalizeTimeout
	if timeout <= 0 {
		timeout = registrationFinalizeTimeout
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	defer clear(pending.ownerSession)
	defer r.removePending(pending)
	interval := r.pollInterval
	if interval <= 0 {
		interval = registrationPollInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		workspace, err := r.reader.workspace(ctx, string(pending.ownerSession), pending.metadata.WorkspaceID)
		if err == nil {
			err = validateRegistrationWorkspace(pending.metadata, workspace, true)
			if err == nil {
				agentID, agentErr := registrationAgent(workspace)
				if agentErr != nil {
					log.Printf("finalize workspace agent registration %s: %v", pending.metadata.WorkspaceID, agentErr)
					return
				}
				binding := workspaceAgentBinding{
					AgentID: agentID, TokenHash: pending.tokenHash,
					workspaceAgentMetadata: pending.metadata,
				}
				if storeErr := r.storePending(pending, binding); storeErr != nil {
					log.Printf("store workspace agent registration %s: %v", pending.metadata.WorkspaceID, storeErr)
				}
				return
			}
			if !errors.Is(err, errRegistrationPending) {
				log.Printf("finalize workspace agent registration %s: %v", pending.metadata.WorkspaceID, err)
				return
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (r *workspaceAgentRegistry) removePending(pending *pendingWorkspaceAgent) {
	r.pendingMu.Lock()
	defer r.pendingMu.Unlock()
	if r.pending[pending.metadata.WorkspaceID] == pending {
		delete(r.pending, pending.metadata.WorkspaceID)
	}
}

func (r *workspaceAgentRegistry) storePending(pending *pendingWorkspaceAgent, next workspaceAgentBinding) error {
	r.pendingMu.Lock()
	defer r.pendingMu.Unlock()
	if r.pending[pending.metadata.WorkspaceID] != pending {
		return errors.New("workspace agent registration was superseded")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	bindings, err := readWorkspaceAgentBindings(r.path)
	if err != nil {
		return err
	}
	filtered := make([]workspaceAgentBinding, 0, len(bindings)+1)
	for _, existing := range bindings {
		if existing.TokenHash == next.TokenHash && !equalWorkspaceAgentMetadata(existing.workspaceAgentMetadata, next.workspaceAgentMetadata) {
			return errors.New("workspace agent token is already bound")
		}
		if existing.WorkspaceID != next.WorkspaceID {
			filtered = append(filtered, existing)
		}
	}
	if len(filtered) >= workspaceAgentBindingLimit {
		return errors.New("workspace agent binding limit exceeded")
	}
	filtered = append(filtered, next)
	sort.Slice(filtered, func(i, j int) bool { return filtered[i].WorkspaceID < filtered[j].WorkspaceID })
	return writeWorkspaceAgentBindings(r.path, filtered)
}

func (r *workspaceAgentRegistry) authenticate(ctx context.Context, token string) (identity, error) {
	if !userID.MatchString(token) {
		return identity{}, errors.New("invalid Coder agent token")
	}
	tokenHash := workspaceAgentTokenHash(token)
	record, cached := registeredWorkspaceAgentBinding(ctx, tokenHash)
	if !cached {
		var err error
		record, err = r.binding(tokenHash)
		if err != nil {
			return identity{}, err
		}
	}
	if err := r.validator.validate(ctx, token); err != nil {
		return identity{}, err
	}
	current, err := r.binding(tokenHash)
	if err != nil || !equalWorkspaceAgentBinding(current, record) {
		return identity{}, errors.New("workspace agent binding changed during authentication")
	}
	isRoot := record.IsRoot == nil || *record.IsRoot
	return identity{
		AgentID: record.AgentID, Cell: record.Cell, Incarnation: record.Incarnation,
		Lineage: record.Lineage, ParentLineage: record.ParentLineage,
		ParentSnapshot: record.ParentSnapshot, IsRoot: isRoot,
		Machine: record.Machine, SubjectID: record.OwnerID,
		Team: record.Team, Workspace: record.WorkspaceName, WorkspaceID: record.WorkspaceID,
		WorkspaceVolume: record.WorkspaceVolume,
	}, nil
}

func (r *workspaceAgentRegistry) binding(tokenHash string) (workspaceAgentBinding, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	bindings, err := r.cachedBindings()
	if err != nil {
		return workspaceAgentBinding{}, err
	}
	match, found := bindings[tokenHash]
	if !found {
		return workspaceAgentBinding{}, errors.New("workspace agent is not registered")
	}
	return match, nil
}

func (r *workspaceAgentRegistry) cachedBindings() (map[string]workspaceAgentBinding, error) {
	current, err := os.Stat(r.path)
	if errors.Is(err, os.ErrNotExist) {
		r.cache = map[string]workspaceAgentBinding{}
		r.cacheFile = nil
		return r.cache, nil
	}
	if err != nil {
		return nil, err
	}
	if r.cacheFile != nil && os.SameFile(r.cacheFile, current) {
		return r.cache, nil
	}
	bindings, opened, err := loadWorkspaceAgentBindings(r.path)
	if err != nil {
		return nil, err
	}
	cache := make(map[string]workspaceAgentBinding, len(bindings))
	for _, binding := range bindings {
		cache[binding.TokenHash] = binding
	}
	r.cache = cache
	r.cacheFile = opened
	return r.cache, nil
}

func workspaceAgentTokenHash(token string) string {
	digest := sha256.Sum256([]byte(workspaceAgentTokenHashDomain + token))
	return hex.EncodeToString(digest[:])
}

func readWorkspaceAgentBindings(path string) ([]workspaceAgentBinding, error) {
	bindings, _, err := loadWorkspaceAgentBindings(path)
	return bindings, err
}

func loadWorkspaceAgentBindings(path string) ([]workspaceAgentBinding, os.FileInfo, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, nil, err
	}
	var state workspaceAgentBindingFile
	if err := decodeBoundedStrictJSON(file, workspaceAgentFileLimit, &state); err != nil || state.Schema != 1 ||
		len(state.Bindings) > workspaceAgentBindingLimit {
		return nil, nil, errors.New("invalid workspace agent binding state")
	}
	workspaces := make(map[string]struct{}, len(state.Bindings))
	tokens := make(map[string]struct{}, len(state.Bindings))
	for _, binding := range state.Bindings {
		if !userID.MatchString(binding.AgentID) || !tokenHash(binding.TokenHash) ||
			validateWorkspaceAgentMetadata(binding.workspaceAgentMetadata) != nil {
			return nil, nil, errors.New("invalid workspace agent binding state")
		}
		if _, exists := workspaces[binding.WorkspaceID]; exists {
			return nil, nil, errors.New("duplicate workspace agent binding")
		}
		if _, exists := tokens[binding.TokenHash]; exists {
			return nil, nil, errors.New("duplicate workspace agent token binding")
		}
		workspaces[binding.WorkspaceID] = struct{}{}
		tokens[binding.TokenHash] = struct{}{}
	}
	return state.Bindings, info, nil
}

func decodeBoundedStrictJSON(reader io.Reader, limit int64, destination any) error {
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return err
	}
	if int64(len(data)) > limit {
		return errors.New("JSON state exceeds its size limit")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return errors.New("JSON state contains trailing data")
	}
	return nil
}

func writeWorkspaceAgentBindings(path string, bindings []workspaceAgentBinding) error {
	return writeDurableJSON(path, workspaceAgentBindingFile{Bindings: bindings, Schema: 1}, workspaceAgentFileLimit)
}

func writeDurableJSON(path string, value any, limit int) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if len(data)+1 > limit {
		return errors.New("durable JSON state exceeds its size limit")
	}
	temporary := path + ".tmp"
	file, err := os.OpenFile(temporary, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	remove := true
	defer func() {
		_ = file.Close()
		if remove {
			_ = os.Remove(temporary)
		}
	}()
	if _, err = file.Write(append(data, '\n')); err != nil {
		return err
	}
	if err = file.Sync(); err != nil {
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	if err = os.Rename(temporary, path); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer directory.Close()
	if err = directory.Sync(); err != nil {
		return err
	}
	remove = false
	return nil
}

func tokenHash(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size
}

func (l *registrationRateLimiter) allow() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	if l.now != nil {
		now = l.now()
	}
	if l.windowStart.IsZero() || now.Sub(l.windowStart) >= registrationRateWindow {
		l.windowStart = now
		l.count = 0
	}
	if l.count >= registrationRateLimit {
		return false
	}
	l.count++
	return true
}

func readReviewerCredential(path string) ([]byte, error) {
	credential, err := os.ReadFile(path)
	if err != nil || len(credential) == 0 || len(credential) > maxBody {
		return nil, errors.New("Kubernetes reviewer credential is unavailable")
	}
	return bytes.TrimSpace(credential), nil
}

func parseTokenReviewResult(body io.Reader, audience, username string) error {
	var review struct {
		Status struct {
			Authenticated bool     `json:"authenticated"`
			Audiences     []string `json:"audiences"`
			User          struct {
				Username string `json:"username"`
			} `json:"user"`
		} `json:"status"`
	}
	if err := decodeBoundedJSON(body, maxBody, &review); err != nil {
		return err
	}
	if !review.Status.Authenticated || review.Status.User.Username != username ||
		len(review.Status.Audiences) != 1 || review.Status.Audiences[0] != audience {
		return errors.New("Kubernetes rejected the provisioner identity")
	}
	return nil
}

func (r kubernetesTokenReviewer) authenticate(ctx context.Context, token, audience, username string) error {
	if token == "" || audience == "" || username == "" || r.client == nil {
		return errors.New("invalid TokenReview request")
	}
	credential, err := readReviewerCredential(r.credentialPath)
	if err != nil {
		return err
	}
	defer clear(credential)
	body, err := json.Marshal(map[string]any{
		"apiVersion": "authentication.k8s.io/v1",
		"kind":       "TokenReview",
		"spec": map[string]any{
			"audiences": []string{audience},
			"token":     token,
		},
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(r.endpoint, "/")+"/apis/authentication.k8s.io/v1/tokenreviews", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+string(credential))
	req.Header.Set("Content-Type", "application/json")
	res, err := r.client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusCreated {
		return fmt.Errorf("Kubernetes returned %s", res.Status)
	}
	return parseTokenReviewResult(res.Body, audience, username)
}

func kubernetesTokenReviewClient(caPath string) (*http.Client, error) {
	certificate, err := os.ReadFile(caPath)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(certificate) {
		return nil, errors.New("invalid Kubernetes CA bundle")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}
	transport.ResponseHeaderTimeout = upstreamTimeout
	transport.TLSHandshakeTimeout = upstreamTimeout
	transport.IdleConnTimeout = serverIdleTimeout
	return &http.Client{
		Transport: transport,
		Timeout:   upstreamTimeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}, nil
}

func bearerToken(value string) string {
	token, found := strings.CutPrefix(value, "Bearer ")
	if !found || token == "" || strings.TrimSpace(token) != token || strings.ContainsAny(token, " \t\r\n") {
		return ""
	}
	return token
}
