// Package main provides a reconciliation controller for Coder workspaces.

package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	defaultRollingWindowSeconds float64 = 6 * 3600.0
	defaultMaxHealsPerWindow    int     = 2
	defaultMaxFleetHealsPerRun  int     = 5
	defaultNamespace            string  = "coder"
	defaultConfigMapName        string  = "coder-workspace-healer-state"
	defaultDataKey              string  = "state.json"
	tokenName                   string  = "local-workspace-healer"
	tokenLifetime               int64   = 6 * 24 * 60 * 60 * 1_000_000_000 // 6 days in nanoseconds
	optOutAnnotation            string  = "healer.coder.openplex.dev/opt-out"
)

var tokenScopes = []string{
	"coder:workspaces.operate",
	"organization:read",
	"user:read",
	"workspace:read",
	"workspace:start",
	"workspace:stop",
	"workspace:update",
}

var truthyValues = map[string]struct{}{
	"true":    {},
	"1":       {},
	"yes":     {},
	"on":      {},
	"enabled": {},
}

var terminalStatuses = map[string]struct{}{
	"running": {},
	"failed":  {},
}

var unhealthyAgentStatuses = map[string]struct{}{
	"disconnected": {},
	"timeout":      {},
}

// Decision represents an evaluation or action taken for a workspace.
type Decision struct {
	Action            string `json:"action"`
	ConsecutiveMisses int    `json:"consecutive_misses"`
	Owner             string `json:"owner"`
	Reason            string `json:"reason"`
	WorkspaceID       string `json:"workspace_id"`
}

// Workspace represents a Coder workspace entity.
type Workspace struct {
	ID                        string                 `json:"id"`
	Name                      string                 `json:"name"`
	OwnerName                 string                 `json:"owner_name"`
	OwnerUsername             string                 `json:"owner_username"`
	OwnerID                   string                 `json:"owner_id"`
	Owner                     *WorkspaceOwner        `json:"owner,omitempty"`
	Annotations               map[string]interface{} `json:"annotations,omitempty"`
	Labels                    map[string]interface{} `json:"labels,omitempty"`
	Tags                      map[string]interface{} `json:"tags,omitempty"`
	Parameters                []WorkspaceParam       `json:"parameters,omitempty"`
	TemplateVersionParameters []WorkspaceParam       `json:"template_version_parameters,omitempty"`
	LatestBuild               *WorkspaceBuild        `json:"latest_build,omitempty"`
	Resources                 []WorkspaceResource    `json:"resources,omitempty"`
	Agents                    []WorkspaceAgent       `json:"agents,omitempty"`
}

// WorkspaceOwner represents the user owning the workspace.
type WorkspaceOwner struct {
	Username string `json:"username"`
}

// WorkspaceBuild represents a workspace build cycle.
type WorkspaceBuild struct {
	ID              string              `json:"id"`
	Transition      string              `json:"transition"`
	Status          string              `json:"status"`
	Job             WorkspaceJob        `json:"job"`
	Resources       []WorkspaceResource `json:"resources,omitempty"`
	Parameters      []WorkspaceParam    `json:"parameters,omitempty"`
	BuildParameters []WorkspaceParam    `json:"build_parameters,omitempty"`
	Agents          []WorkspaceAgent    `json:"agents,omitempty"`
}

// WorkspaceJob describes provisioner status and error messages.
type WorkspaceJob struct {
	Error  string `json:"error,omitempty"`
	Status string `json:"status,omitempty"`
}

// WorkspaceResource holds resource-level agents and metadata.
type WorkspaceResource struct {
	Name     string           `json:"name,omitempty"`
	Agents   []WorkspaceAgent `json:"agents,omitempty"`
	Metadata []WorkspaceParam `json:"metadata,omitempty"`
}

// WorkspaceAgent describes a connected or disconnected compute agent.
type WorkspaceAgent struct {
	ID     string `json:"id"`
	Name   string `json:"name,omitempty"`
	Status string `json:"status,omitempty"`
}

// WorkspaceParam captures generic name/key-value metadata.
type WorkspaceParam struct {
	Key   string      `json:"key,omitempty"`
	Name  string      `json:"name,omitempty"`
	Value interface{} `json:"value,omitempty"`
}

// WorkspaceState records historical health tracking for a single workspace.
type WorkspaceState struct {
	FirstMissAt *float64  `json:"first_miss_at"`
	HealHistory []float64 `json:"heal_history"`
	LastMissAt  *float64  `json:"last_miss_at"`
	MissCount   int       `json:"miss_count"`
}

// HealerState holds the serialized top-level persistent state structure.
type HealerState struct {
	UpdatedAt  float64                    `json:"updated_at"`
	Version    int                        `json:"version"`
	Workspaces map[string]*WorkspaceState `json:"workspaces"`
}

// BootstrapError signals an issue during OIDC or Coder authentication bootstrap.
type BootstrapError struct {
	Message string
}

func (e *BootstrapError) Error() string {
	return e.Message
}

// StateBackend defines storage abstraction for persisting healer state.
type StateBackend interface {
	Load(ctx context.Context) (map[string]*WorkspaceState, error)
	Save(ctx context.Context, data map[string]*WorkspaceState, now float64) error
}

// InMemoryBackend provides volatile in-memory state tracking.
type InMemoryBackend struct {
	mu   sync.Mutex
	data map[string]*WorkspaceState
}

// NewInMemoryBackend creates an empty in-memory backend.
func NewInMemoryBackend() *InMemoryBackend {
	return &InMemoryBackend{data: make(map[string]*WorkspaceState)}
}

// Load retrieves a copy of stored workspace states.
func (b *InMemoryBackend) Load(_ context.Context) (map[string]*WorkspaceState, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	res := make(map[string]*WorkspaceState, len(b.data))
	for k, v := range b.data {
		cp := *v
		cp.HealHistory = append([]float64(nil), v.HealHistory...)
		res[k] = &cp
	}
	return res, nil
}

// Save persists workspace states in memory.
func (b *InMemoryBackend) Save(_ context.Context, data map[string]*WorkspaceState, _ float64) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.data = make(map[string]*WorkspaceState, len(data))
	for k, v := range data {
		cp := *v
		cp.HealHistory = append([]float64(nil), v.HealHistory...)
		b.data[k] = &cp
	}
	return nil
}

// FileBackend persists state to a local JSON file atomically.
type FileBackend struct {
	Path string
}

// Load parses state from the local JSON file.
func (b *FileBackend) Load(_ context.Context) (map[string]*WorkspaceState, error) {
	content, err := os.ReadFile(b.Path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	if len(bytes.TrimSpace(content)) == 0 {
		return nil, nil
	}
	return deserializeState(content)
}

// Save atomically writes state to the local JSON file.
func (b *FileBackend) Save(_ context.Context, data map[string]*WorkspaceState, now float64) error {
	dir := filepath.Dir(b.Path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("failed to create state directory: %w", err)
	}
	serialized, err := serializeState(data, now)
	if err != nil {
		return err
	}
	tmpFile := fmt.Sprintf("%s.tmp.%d", b.Path, os.Getpid())
	if err := os.WriteFile(tmpFile, serialized, 0644); err != nil {
		_ = os.Remove(tmpFile)
		return fmt.Errorf("failed to write state file %s: %w", b.Path, err)
	}
	if err := os.Rename(tmpFile, b.Path); err != nil {
		_ = os.Remove(tmpFile)
		return fmt.Errorf("failed to rename state file to %s: %w", b.Path, err)
	}
	return nil
}

// HTTPDoer abstracts HTTP requests for testing.
type HTTPDoer interface {
	Do(req *http.Request) (*http.Response, error)
}

// ConfigMapBackend persists state into a Kubernetes ConfigMap.
type ConfigMapBackend struct {
	Client    HTTPDoer
	DataKey   string
	Host      string
	Name      string
	Namespace string
	Port      string
	Token     string
}

// NewConfigMapBackend initializes a Kubernetes ConfigMap backend.
func NewConfigMapBackend(name, namespace, dataKey string, client HTTPDoer) *ConfigMapBackend {
	if name == "" {
		name = defaultConfigMapName
	}
	if namespace == "" {
		namespace = defaultNamespace
	}
	if dataKey == "" {
		dataKey = defaultDataKey
	}
	return &ConfigMapBackend{
		Client:    client,
		DataKey:   dataKey,
		Name:      name,
		Namespace: namespace,
	}
}

func (b *ConfigMapBackend) ensureClient() (HTTPDoer, string, error) {
	if b.Client != nil {
		return b.Client, "https://kubernetes.default.svc:443", nil
	}

	host := b.Host
	if host == "" {
		host = os.Getenv("KUBERNETES_SERVICE_HOST")
	}
	if host == "" {
		host = "kubernetes.default.svc"
	}
	port := b.Port
	if port == "" {
		port = os.Getenv("KUBERNETES_SERVICE_PORT_HTTPS")
	}
	if port == "" {
		port = os.Getenv("KUBERNETES_SERVICE_PORT")
	}
	if port == "" {
		port = "443"
	}
	baseURL := fmt.Sprintf("https://%s:%s", host, port)

	token := b.Token
	if token == "" {
		for _, tokenPath := range []string{
			"/var/run/secrets/kubernetes.io/serviceaccount/token",
			"/var/run/coder/oauth-bootstrap/token",
		} {
			if data, err := os.ReadFile(tokenPath); err == nil {
				token = strings.TrimSpace(string(data))
				break
			}
		}
	}
	b.Token = token

	caPool, _ := x509.SystemCertPool()
	if caPool == nil {
		caPool = x509.NewCertPool()
	}
	for _, caPath := range []string{
		"/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
		"/var/run/coder/oauth-bootstrap/ca.crt",
	} {
		if caData, err := os.ReadFile(caPath); err == nil {
			caPool.AppendCertsFromPEM(caData)
		}
	}

	transport := &http.Transport{
		TLSClientConfig: &tls.Config{
			RootCAs:    caPool,
			MinVersion: tls.VersionTLS12,
		},
	}
	b.Client = &http.Client{
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
		Timeout:   20 * time.Second,
		Transport: transport,
	}
	return b.Client, baseURL, nil
}

// Load reads and parses state from the ConfigMap.
func (b *ConfigMapBackend) Load(ctx context.Context) (map[string]*WorkspaceState, error) {
	client, baseURL, err := b.ensureClient()
	if err != nil {
		return nil, err
	}
	path := fmt.Sprintf("%s/api/v1/namespaces/%s/configmaps/%s", baseURL, b.Namespace, b.Name)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if b.Token != "" {
		req.Header.Set("Authorization", "Bearer "+b.Token)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("kubernetes API communication error for GET %s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("kubernetes API returned HTTP %d for GET %s", resp.StatusCode, path)
	}

	var cm struct {
		Data map[string]string `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&cm); err != nil {
		return nil, fmt.Errorf("failed to decode JSON from ConfigMap '%s': %w", b.Name, err)
	}
	rawContent, ok := cm.Data[b.DataKey]
	if !ok || strings.TrimSpace(rawContent) == "" {
		return nil, nil
	}
	return deserializeState([]byte(rawContent))
}

// Save patches or creates the ConfigMap containing state JSON.
func (b *ConfigMapBackend) Save(ctx context.Context, data map[string]*WorkspaceState, now float64) error {
	client, baseURL, err := b.ensureClient()
	if err != nil {
		return err
	}
	serialized, err := serializeState(data, now)
	if err != nil {
		return err
	}

	patchPayload := map[string]interface{}{
		"apiVersion": "v1",
		"data": map[string]string{
			b.DataKey: string(serialized),
		},
		"kind": "ConfigMap",
		"metadata": map[string]string{
			"name":      b.Name,
			"namespace": b.Namespace,
		},
	}
	patchBody, err := json.Marshal(patchPayload)
	if err != nil {
		return err
	}

	path := fmt.Sprintf("%s/api/v1/namespaces/%s/configmaps/%s", baseURL, b.Namespace, b.Name)
	req, err := http.NewRequestWithContext(ctx, http.MethodPatch, path, bytes.NewReader(patchBody))
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/merge-patch+json")
	if b.Token != "" {
		req.Header.Set("Authorization", "Bearer "+b.Token)
	}

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("kubernetes API communication error for PATCH %s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusOK {
		return nil
	}
	if resp.StatusCode != http.StatusNotFound {
		return fmt.Errorf("kubernetes API returned unexpected status %d for PATCH %s", resp.StatusCode, path)
	}

	// Create ConfigMap if it did not exist
	createPath := fmt.Sprintf("%s/api/v1/namespaces/%s/configmaps", baseURL, b.Namespace)
	createReq, err := http.NewRequestWithContext(ctx, http.MethodPost, createPath, bytes.NewReader(patchBody))
	if err != nil {
		return err
	}
	createReq.Header.Set("Accept", "application/json")
	createReq.Header.Set("Content-Type", "application/json")
	if b.Token != "" {
		createReq.Header.Set("Authorization", "Bearer "+b.Token)
	}

	createResp, err := client.Do(createReq)
	if err != nil {
		return fmt.Errorf("kubernetes API communication error for POST %s: %w", createPath, err)
	}
	defer createResp.Body.Close()

	if createResp.StatusCode != http.StatusOK && createResp.StatusCode != http.StatusCreated {
		return fmt.Errorf("kubernetes API returned unexpected status %d for POST %s", createResp.StatusCode, createPath)
	}
	return nil
}

// StateTracker manages consecutive miss records, heal histories, and rate limiting.
type StateTracker struct {
	backend              StateBackend
	currentRunHeals      int
	maxFleetHealsPerRun  int
	maxHealsPerWindow    int
	mu                   sync.Mutex
	rollingWindowSeconds float64
	workspaces           map[string]*WorkspaceState
}

// NewStateTracker constructs a new state tracker with configuration options.
func NewStateTracker(backend StateBackend, maxHealsPerWindow int, rollingWindowSeconds float64, maxFleetHealsPerRun int) *StateTracker {
	if backend == nil {
		backend = NewInMemoryBackend()
	}
	if maxHealsPerWindow <= 0 {
		maxHealsPerWindow = defaultMaxHealsPerWindow
	}
	if rollingWindowSeconds <= 0 {
		rollingWindowSeconds = defaultRollingWindowSeconds
	}
	if maxFleetHealsPerRun <= 0 {
		maxFleetHealsPerRun = defaultMaxFleetHealsPerRun
	}
	return &StateTracker{
		backend:              backend,
		maxFleetHealsPerRun:  maxFleetHealsPerRun,
		maxHealsPerWindow:    maxHealsPerWindow,
		rollingWindowSeconds: rollingWindowSeconds,
		workspaces:           make(map[string]*WorkspaceState),
	}
}

// CurrentRunHeals returns the count of heals executed in the active run.
func (t *StateTracker) CurrentRunHeals() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.currentRunHeals
}

// ResetRunCounters zeroes out the run heal count for a new reconciliation pass.
func (t *StateTracker) ResetRunCounters() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.currentRunHeals = 0
}

func (t *StateTracker) getOrCreate(workspaceID string) *WorkspaceState {
	ws, ok := t.workspaces[workspaceID]
	if !ok {
		ws = &WorkspaceState{}
		t.workspaces[workspaceID] = ws
	}
	return ws
}

// RecordMiss registers a health failure miss for a workspace.
func (t *StateTracker) RecordMiss(workspaceID string, timestamp ...float64) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := float64(time.Now().UnixNano()) / 1e9
	if len(timestamp) > 0 && timestamp[0] > 0 {
		now = timestamp[0]
	}
	ws := t.getOrCreate(workspaceID)
	ws.MissCount++
	if ws.FirstMissAt == nil {
		ws.FirstMissAt = &now
	}
	ws.LastMissAt = &now
	return ws.MissCount
}

// RecordConnected clears consecutive misses for a workspace.
func (t *StateTracker) RecordConnected(workspaceID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if ws, ok := t.workspaces[workspaceID]; ok {
		ws.MissCount = 0
		ws.FirstMissAt = nil
		ws.LastMissAt = nil
	}
}

// RecordHealthy aliases RecordConnected.
func (t *StateTracker) RecordHealthy(workspaceID string) {
	t.RecordConnected(workspaceID)
}

// GetMissCount returns consecutive misses recorded for a workspace.
func (t *StateTracker) GetMissCount(workspaceID string) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	if ws, ok := t.workspaces[workspaceID]; ok {
		return ws.MissCount
	}
	return 0
}

// GetMisses aliases GetMissCount.
func (t *StateTracker) GetMisses(workspaceID string) int {
	return t.GetMissCount(workspaceID)
}

// GetMissTimestamps returns first and last miss timestamps.
func (t *StateTracker) GetMissTimestamps(workspaceID string) (*float64, *float64) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if ws, ok := t.workspaces[workspaceID]; ok {
		return ws.FirstMissAt, ws.LastMissAt
	}
	return nil, nil
}

// GetHealHistory returns recent heal timestamps for a workspace.
func (t *StateTracker) GetHealHistory(workspaceID string) []float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	if ws, ok := t.workspaces[workspaceID]; ok {
		return append([]float64(nil), ws.HealHistory...)
	}
	return nil
}

// CanHeal evaluates whether a workspace is permitted to heal under limits.
func (t *StateTracker) CanHeal(workspaceID string, timestamp ...float64) bool {
	can, _ := t.CanHealWithReason(workspaceID, timestamp...)
	return can
}

// CanHealWithReason evaluates rate limits and returns an explanatory status.
func (t *StateTracker) CanHealWithReason(workspaceID string, timestamp ...float64) (bool, string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := float64(time.Now().UnixNano()) / 1e9
	if len(timestamp) > 0 && timestamp[0] > 0 {
		now = timestamp[0]
	}
	var history []float64
	if ws, ok := t.workspaces[workspaceID]; ok {
		history = ws.HealHistory
	}
	return evaluateHealingRateLimits(
		history,
		t.currentRunHeals,
		t.maxFleetHealsPerRun,
		t.maxHealsPerWindow,
		t.rollingWindowSeconds,
		now,
	)
}

// RecordHeal logs an executed heal event against a workspace and fleet counter.
func (t *StateTracker) RecordHeal(workspaceID string, timestamp ...float64) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := float64(time.Now().UnixNano()) / 1e9
	if len(timestamp) > 0 && timestamp[0] > 0 {
		now = timestamp[0]
	}
	ws := t.getOrCreate(workspaceID)
	ws.HealHistory = append(ws.HealHistory, now)
	t.currentRunHeals++
}

// PruneInactiveWorkspaces removes records for absent workspaces lacking history.
func (t *StateTracker) PruneInactiveWorkspaces(activeIDs map[string]struct{}) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	pruned := 0
	for wid, ws := range t.workspaces {
		if _, ok := activeIDs[wid]; !ok && len(ws.HealHistory) == 0 {
			delete(t.workspaces, wid)
			pruned++
		}
	}
	return pruned
}

// Prune removes deleted workspaces from tracking.
func (t *StateTracker) Prune(activeIDs map[string]struct{}) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	pruned := 0
	for wid := range t.workspaces {
		if _, ok := activeIDs[wid]; !ok {
			delete(t.workspaces, wid)
			pruned++
		}
	}
	return pruned
}

// PruneHealHistory drops timestamps outside the retention window.
func (t *StateTracker) PruneHealHistory(olderThanSeconds float64, timestamp ...float64) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := float64(time.Now().UnixNano()) / 1e9
	if len(timestamp) > 0 && timestamp[0] > 0 {
		now = timestamp[0]
	}
	retention := olderThanSeconds
	if retention <= 0 {
		retention = t.rollingWindowSeconds
	}
	cutoff := now - retention
	pruned := 0
	for _, ws := range t.workspaces {
		var kept []float64
		for _, ts := range ws.HealHistory {
			if ts >= cutoff {
				kept = append(kept, ts)
			} else {
				pruned++
			}
		}
		ws.HealHistory = kept
	}
	return pruned
}

// Load reloads tracker state from the configured backend.
func (t *StateTracker) Load(ctx context.Context) error {
	data, err := t.backend.Load(ctx)
	if err != nil {
		return err
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if data != nil {
		t.workspaces = data
	}
	return nil
}

// Save writes current tracker state to the backend.
func (t *StateTracker) Save(ctx context.Context, timestamp ...float64) error {
	t.mu.Lock()
	now := float64(time.Now().UnixNano()) / 1e9
	if len(timestamp) > 0 && timestamp[0] > 0 {
		now = timestamp[0]
	}
	snapshot := make(map[string]*WorkspaceState, len(t.workspaces))
	for k, v := range t.workspaces {
		cp := *v
		cp.HealHistory = append([]float64(nil), v.HealHistory...)
		snapshot[k] = &cp
	}
	t.mu.Unlock()
	return t.backend.Save(ctx, snapshot, now)
}

// ToJSON formats state as a JSON string.
func (t *StateTracker) ToJSON(timestamp ...float64) (string, error) {
	t.mu.Lock()
	now := float64(time.Now().UnixNano()) / 1e9
	if len(timestamp) > 0 && timestamp[0] > 0 {
		now = timestamp[0]
	}
	bytesData, err := serializeState(t.workspaces, now)
	t.mu.Unlock()
	return string(bytesData), err
}

// FromJSON parses state JSON into the tracker.
func (t *StateTracker) FromJSON(jsonStr string) error {
	data, err := deserializeState([]byte(jsonStr))
	if err != nil {
		return err
	}
	t.mu.Lock()
	t.workspaces = data
	t.mu.Unlock()
	return nil
}

func countHealsInWindow(healTimestamps []float64, windowSeconds float64, now float64) int {
	cutoff := now - windowSeconds
	count := 0
	for _, ts := range healTimestamps {
		if cutoff <= ts && ts <= now {
			count++
		}
	}
	return count
}

func evaluateHealingRateLimits(
	workspaceHeals []float64,
	currentRunHeals int,
	maxFleetHeals int,
	maxWorkspaceHeals int,
	windowSeconds float64,
	now float64,
) (bool, string) {
	if currentRunHeals >= maxFleetHeals {
		return false, fmt.Sprintf(
			"fleet concurrency limit reached (%d/%d heals in current run)",
			currentRunHeals, maxFleetHeals,
		)
	}

	recentHeals := countHealsInWindow(workspaceHeals, windowSeconds, now)
	if recentHeals >= maxWorkspaceHeals {
		windowHours := windowSeconds / 3600.0
		return false, fmt.Sprintf(
			"workspace heal rate limit reached (%d/%d heals in rolling %gh window)",
			recentHeals, maxWorkspaceHeals, windowHours,
		)
	}

	return true, "permitted"
}

func serializeState(workspaces map[string]*WorkspaceState, now float64) ([]byte, error) {
	state := HealerState{
		UpdatedAt:  now,
		Version:    1,
		Workspaces: workspaces,
	}
	return json.MarshalIndent(state, "", "  ")
}

func deserializeState(data []byte) (map[string]*WorkspaceState, error) {
	var hs struct {
		Workspaces map[string]*WorkspaceState `json:"workspaces"`
	}
	if err := json.Unmarshal(data, &hs); err == nil && hs.Workspaces != nil {
		return hs.Workspaces, nil
	}

	var rawMap map[string]json.RawMessage
	if err := json.Unmarshal(data, &rawMap); err != nil {
		return nil, fmt.Errorf("invalid state data structure: expected JSON object: %w", err)
	}
	result := make(map[string]*WorkspaceState)
	for k, raw := range rawMap {
		var ws WorkspaceState
		if err := json.Unmarshal(raw, &ws); err == nil && (ws.MissCount != 0 || ws.FirstMissAt != nil || len(ws.HealHistory) > 0) {
			result[k] = &ws
			continue
		}
		var count int
		if err := json.Unmarshal(raw, &count); err == nil {
			result[k] = &WorkspaceState{MissCount: count}
		}
	}
	return result, nil
}

// RateLimiter bounds HTTP request frequency.
type RateLimiter struct {
	interval time.Duration
	lastCall time.Time
	mu       sync.Mutex
}

// NewRateLimiter initializes a rate limiter with requests per second.
func NewRateLimiter(rps float64) *RateLimiter {
	var interval time.Duration
	if rps > 0 {
		interval = time.Duration(float64(time.Second) / rps)
	}
	return &RateLimiter{interval: interval}
}

// Acquire blocks until a call permit is available.
func (r *RateLimiter) Acquire(ctx context.Context) error {
	if r == nil || r.interval <= 0 {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	elapsed := now.Sub(r.lastCall)
	if elapsed < r.interval {
		wait := r.interval - elapsed
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
	}
	r.lastCall = time.Now()
	return nil
}

func origin(rawURL string) (string, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return "", fmt.Errorf("invalid bootstrap URL: %s", rawURL)
	}
	return fmt.Sprintf("%s://%s", parsed.Scheme, parsed.Host), nil
}

func requireHTTPS(rawURL string, allowInsecure bool) error {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return err
	}
	if parsed.Scheme != "https" && !allowInsecure {
		return &BootstrapError{Message: "OIDC bootstrap endpoints must use HTTPS"}
	}
	return nil
}

type loginForm struct {
	action string
	fields map[string]string
}

func parseLoginForm(htmlStr string) (loginForm, error) {
	form := loginForm{fields: make(map[string]string)}
	formRe := regexp.MustCompile(`(?i)<form\b([^>]*)>`)
	inputRe := regexp.MustCompile(`(?i)<input\b([^>]*)>`)
	attrRe := regexp.MustCompile(`(?i)([a-zA-Z0-9_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))`)

	formMatch := formRe.FindStringSubmatch(htmlStr)
	if len(formMatch) < 2 {
		return form, &BootstrapError{Message: "OIDC issuer did not provide its login form"}
	}
	for _, attr := range attrRe.FindAllStringSubmatch(formMatch[1], -1) {
		name := strings.ToLower(attr[1])
		val := attr[2]
		if val == "" {
			val = attr[3]
		}
		if val == "" {
			val = attr[4]
		}
		if name == "action" {
			form.action = html.UnescapeString(val)
		}
	}
	if form.action == "" {
		return form, &BootstrapError{Message: "OIDC issuer did not provide its login form"}
	}

	for _, inputMatch := range inputRe.FindAllStringSubmatch(htmlStr, -1) {
		attrs := make(map[string]string)
		for _, attr := range attrRe.FindAllStringSubmatch(inputMatch[1], -1) {
			name := strings.ToLower(attr[1])
			val := attr[2]
			if val == "" {
				val = attr[3]
			}
			if val == "" {
				val = attr[4]
			}
			attrs[name] = html.UnescapeString(val)
		}
		inputType := strings.ToLower(attrs["type"])
		if inputType == "submit" || inputType == "button" {
			continue
		}
		if name, ok := attrs["name"]; ok && name != "" {
			form.fields[name] = attrs["value"]
		}
	}
	return form, nil
}

// BrowserLogin performs the automated OIDC authentication sequence against Dex.
func BrowserLogin(ctx context.Context, coderURL, issuerURL, username, password string, allowInsecure bool, customClient ...HTTPDoer) (string, error) {
	if err := requireHTTPS(coderURL, allowInsecure); err != nil {
		return "", err
	}
	if err := requireHTTPS(issuerURL, allowInsecure); err != nil {
		return "", err
	}

	coderOrigin, err := origin(coderURL)
	if err != nil {
		return "", err
	}
	issuerOrigin, err := origin(issuerURL)
	if err != nil {
		return "", err
	}
	allowedOrigins := map[string]struct{}{
		coderOrigin:  {},
		issuerOrigin: {},
	}

	jar, _ := cookiejar.New(nil)
	var client HTTPDoer
	if len(customClient) > 0 && customClient[0] != nil {
		client = customClient[0]
	} else {
		client = &http.Client{
			CheckRedirect: func(req *http.Request, _ []*http.Request) error {
				targetOrigin, err := origin(req.URL.String())
				if err != nil {
					return &BootstrapError{Message: err.Error()}
				}
				if _, ok := allowedOrigins[targetOrigin]; !ok {
					return &BootstrapError{
						Message: fmt.Sprintf("OIDC browser flow attempted off-target redirect to %s", targetOrigin),
					}
				}
				return nil
			},
			Jar:     jar,
			Timeout: 20 * time.Second,
		}
	}

	loginEntrypoint := fmt.Sprintf("%s/api/v2/users/oidc/callback?redirect=%%2F", strings.TrimRight(coderURL, "/"))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, loginEntrypoint, nil)
	if err != nil {
		return "", err
	}

	resp, err := client.Do(req)
	if err != nil {
		var bErr *BootstrapError
		if errors.As(err, &bErr) {
			return "", bErr
		}
		return "", &BootstrapError{Message: "Coder did not start the OIDC browser flow"}
	}
	defer resp.Body.Close()

	loginURL := resp.Request.URL.String()
	loginOrigin, err := origin(loginURL)
	if err != nil || loginOrigin != issuerOrigin {
		return "", &BootstrapError{Message: "Coder did not redirect to the declared OIDC issuer"}
	}

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return "", &BootstrapError{Message: "Coder did not start the OIDC browser flow"}
	}

	form, err := parseLoginForm(string(bodyBytes))
	if err != nil {
		return "", err
	}

	loginActionURL, err := url.Parse(form.action)
	if err != nil {
		return "", &BootstrapError{Message: "OIDC login form attempted to leave its issuer"}
	}
	resolvedAction := resp.Request.URL.ResolveReference(loginActionURL).String()
	actionOrigin, err := origin(resolvedAction)
	if err != nil || actionOrigin != issuerOrigin {
		return "", &BootstrapError{Message: "OIDC login form attempted to leave its issuer"}
	}

	formValues := url.Values{}
	for k, v := range form.fields {
		formValues.Set(k, v)
	}
	formValues.Set("login", username)
	formValues.Set("password", password)

	postReq, err := http.NewRequestWithContext(ctx, http.MethodPost, resolvedAction, strings.NewReader(formValues.Encode()))
	if err != nil {
		return "", err
	}
	postReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	postResp, err := client.Do(postReq)
	if err != nil {
		var bErr *BootstrapError
		if errors.As(err, &bErr) {
			return "", bErr
		}
		return "", &BootstrapError{Message: "OIDC fixture authentication was rejected"}
	}
	defer postResp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(postResp.Body, 64*1024))

	if postResp.StatusCode >= 400 {
		return "", &BootstrapError{Message: "OIDC fixture authentication was rejected"}
	}

	u, _ := url.Parse(coderURL)
	var sessionToken string
	for _, cookie := range jar.Cookies(u) {
		if cookie.Name == "coder_session_token" && cookie.Value != "" {
			sessionToken = cookie.Value
			break
		}
	}
	if sessionToken == "" {
		return "", &BootstrapError{Message: "OIDC flow did not create one Coder session"}
	}
	return sessionToken, nil
}

// RequireOwner validates the current session user holds owner privileges.
func RequireOwner(ctx context.Context, api *CoderClient, username string) error {
	status, body, err := api.Request(ctx, http.MethodGet, "/api/v2/users/me", nil, map[int]struct{}{http.StatusOK: {}})
	if err != nil || status != http.StatusOK {
		return &BootstrapError{Message: "Coder returned an invalid current user"}
	}

	var user struct {
		Email     string `json:"email"`
		LoginType string `json:"login_type"`
		Roles     []struct {
			Name string `json:"name"`
		} `json:"roles"`
	}
	if err := json.Unmarshal(body, &user); err != nil {
		return &BootstrapError{Message: "Coder returned an invalid current user"}
	}

	hasOwnerRole := false
	for _, r := range user.Roles {
		if r.Name == "owner" {
			hasOwnerRole = true
			break
		}
	}
	if user.Email != username || user.LoginType != "oidc" || !hasOwnerRole {
		return &BootstrapError{Message: "local OIDC fixture is not the Coder owner"}
	}
	return nil
}

// MintScopedToken issues an API token specifically scoped for healer operations.
func MintScopedToken(ctx context.Context, api *CoderClient, username string) (string, error) {
	if err := RequireOwner(ctx, api, username); err != nil {
		return "", err
	}

	status, body, err := api.Request(
		ctx,
		http.MethodGet,
		fmt.Sprintf("/api/v2/users/me/keys/tokens/%s", url.PathEscape(tokenName)),
		nil,
		map[int]struct{}{http.StatusOK: {}, http.StatusNotFound: {}},
	)
	if err == nil && status == http.StatusOK {
		var oldToken struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(body, &oldToken); err == nil && oldToken.ID != "" {
			_, _, _ = api.Request(
				ctx,
				http.MethodDelete,
				fmt.Sprintf("/api/v2/users/me/keys/%s", url.PathEscape(oldToken.ID)),
				nil,
				map[int]struct{}{http.StatusNoContent: {}},
			)
		}
	}

	mintPayload := map[string]interface{}{
		"lifetime":   tokenLifetime,
		"scopes":     tokenScopes,
		"token_name": tokenName,
	}
	mintStatus, mintBody, err := api.Request(
		ctx,
		http.MethodPost,
		"/api/v2/users/me/keys/tokens",
		mintPayload,
		map[int]struct{}{http.StatusCreated: {}},
	)
	if err != nil || mintStatus != http.StatusCreated {
		return "", &BootstrapError{Message: "Coder did not return the scoped healer token"}
	}

	var newToken struct {
		Key string `json:"key"`
	}
	if err := json.Unmarshal(mintBody, &newToken); err != nil || strings.TrimSpace(newToken.Key) == "" {
		return "", &BootstrapError{Message: "Coder returned an invalid scoped healer token"}
	}
	return strings.TrimSpace(newToken.Key), nil
}

// Logout invalidates a Coder session token.
func Logout(ctx context.Context, api *CoderClient) error {
	status, _, err := api.Request(ctx, http.MethodPost, "/api/v2/users/logout", nil, map[int]struct{}{http.StatusOK: {}})
	if err != nil || status != http.StatusOK {
		return &BootstrapError{Message: "Coder logout failed"}
	}
	return nil
}

func writeTokenSafely(path, token string) error {
	_ = os.Remove(path)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0440)
	if err != nil {
		return fmt.Errorf("scoped token file could not be created: %w", err)
	}
	defer f.Close()
	if _, err := f.WriteString(token); err != nil {
		return fmt.Errorf("scoped token file could not be written: %w", err)
	}
	return f.Chmod(0440)
}

func readTokenSafely(tokenFile string, unlink bool) (string, error) {
	realPath, err := filepath.EvalSymlinks(tokenFile)
	if err != nil {
		return "", fmt.Errorf("cannot open token file: %w", err)
	}
	info, err := os.Stat(realPath)
	if err != nil {
		return "", fmt.Errorf("cannot open token file: %w", err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("token file must be a regular file: %s", tokenFile)
	}
	if info.Size() > 1024*1024 {
		return "", fmt.Errorf("token file exceeds maximum allowed size: %s", tokenFile)
	}
	data, err := os.ReadFile(realPath)
	if err != nil {
		return "", fmt.Errorf("cannot open token file: %w", err)
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return "", fmt.Errorf("token file is empty: %s", tokenFile)
	}
	if unlink {
		if err := os.Remove(tokenFile); err != nil {
			return "", fmt.Errorf("failed to unlink token file: %w", err)
		}
	}
	return token, nil
}

// CoderClient provides authenticated Coder REST API calls.
type CoderClient struct {
	BaseURL     string
	Client      HTTPDoer
	RateLimiter *RateLimiter
	Token       string
}

// NewCoderClient constructs a new Coder API client.
func NewCoderClient(baseURL, token string, limiter *RateLimiter, client ...HTTPDoer) *CoderClient {
	var c HTTPDoer
	if len(client) > 0 && client[0] != nil {
		c = client[0]
	} else {
		c = &http.Client{
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
			Timeout: 20 * time.Second,
		}
	}
	return &CoderClient{
		BaseURL:     strings.TrimRight(baseURL, "/"),
		Client:      c,
		RateLimiter: limiter,
		Token:       token,
	}
}

// Request executes an authenticated API request and checks the response status code.
func (c *CoderClient) Request(ctx context.Context, method, path string, payload interface{}, accepted map[int]struct{}) (int, []byte, error) {
	if c.RateLimiter != nil {
		_ = c.RateLimiter.Acquire(ctx)
	}

	targetURL := fmt.Sprintf("%s/%s", c.BaseURL, strings.TrimLeft(path, "/"))
	var bodyReader io.Reader
	if payload != nil {
		data, err := json.Marshal(payload)
		if err != nil {
			return 0, nil, err
		}
		bodyReader = bytes.NewReader(data)
	}

	req, err := http.NewRequestWithContext(ctx, method, targetURL, bodyReader)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("Coder-Session-Token", c.Token)
	}

	resp, err := c.Client.Do(req)
	if err != nil {
		return 0, nil, &BootstrapError{Message: fmt.Sprintf("Coder API %s was unreachable: %v", path, err)}
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1024*1024))
	if err != nil {
		return resp.StatusCode, nil, err
	}

	if _, ok := accepted[resp.StatusCode]; !ok {
		return resp.StatusCode, body, &BootstrapError{
			Message: fmt.Sprintf("Coder API %s returned HTTP %d", path, resp.StatusCode),
		}
	}
	return resp.StatusCode, body, nil
}

// ListWorkspaces fetches the workspace inventory from Coder.
func (c *CoderClient) ListWorkspaces(ctx context.Context) ([]Workspace, error) {
	status, body, err := c.Request(ctx, http.MethodGet, "/api/v2/workspaces", nil, map[int]struct{}{http.StatusOK: {}})
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d", status)
	}

	var wrapper struct {
		Workspaces []Workspace `json:"workspaces"`
	}
	if err := json.Unmarshal(body, &wrapper); err == nil && wrapper.Workspaces != nil {
		return wrapper.Workspaces, nil
	}

	var list []Workspace
	if err := json.Unmarshal(body, &list); err == nil {
		return list, nil
	}
	return nil, errors.New("failed to parse workspaces list")
}

// RestartWorkspace initiates a start build for a workspace.
func (c *CoderClient) RestartWorkspace(ctx context.Context, workspaceID string) error {
	path := fmt.Sprintf("/api/v2/workspaces/%s/builds", workspaceID)
	payload := map[string]string{"transition": "start"}
	status, _, err := c.Request(ctx, http.MethodPost, path, payload, map[int]struct{}{
		http.StatusOK:    {},
		http.StatusCreated: {},
	})
	if err != nil {
		return err
	}
	if status != http.StatusOK && status != http.StatusCreated {
		return fmt.Errorf("failed to restart workspace: HTTP %d", status)
	}
	return nil
}

func isTruthyOptOut(key, val interface{}) bool {
	kStr := strings.TrimSpace(strings.ToLower(fmt.Sprint(key)))
	vStr := strings.TrimSpace(strings.ToLower(fmt.Sprint(val)))
	matchesKey := kStr == optOutAnnotation || (strings.Contains(kStr, "opt-out") && strings.Contains(kStr, "healer"))
	_, truthy := truthyValues[vStr]
	return matchesKey && truthy
}

func hasOptOutInParams(params []WorkspaceParam) bool {
	for _, p := range params {
		k := p.Name
		if k == "" {
			k = p.Key
		}
		if isTruthyOptOut(k, p.Value) {
			return true
		}
	}
	return false
}

// IsOptedOut evaluates annotations and parameters to check opt-out directives.
func IsOptedOut(ws *Workspace) bool {
	checkMap := func(m map[string]interface{}) bool {
		for k, v := range m {
			if isTruthyOptOut(k, v) {
				return true
			}
		}
		return false
	}
	if checkMap(ws.Annotations) || checkMap(ws.Labels) || checkMap(ws.Tags) {
		return true
	}
	if hasOptOutInParams(ws.Parameters) || hasOptOutInParams(ws.TemplateVersionParameters) {
		return true
	}
	if ws.LatestBuild != nil {
		if hasOptOutInParams(ws.LatestBuild.Parameters) || hasOptOutInParams(ws.LatestBuild.BuildParameters) {
			return true
		}
		for _, r := range ws.LatestBuild.Resources {
			if hasOptOutInParams(r.Metadata) {
				return true
			}
		}
	}
	return false
}

func getWorkspaceOwner(ws *Workspace) string {
	if ws.OwnerName != "" {
		return ws.OwnerName
	}
	if ws.OwnerUsername != "" {
		return ws.OwnerUsername
	}
	if ws.Owner != nil && ws.Owner.Username != "" {
		return ws.Owner.Username
	}
	if ws.OwnerID != "" {
		return ws.OwnerID
	}
	return "unknown"
}

func getWorkspaceAgents(ws *Workspace) []WorkspaceAgent {
	var candidates []WorkspaceAgent
	if ws.LatestBuild != nil {
		for _, r := range ws.LatestBuild.Resources {
			candidates = append(candidates, r.Agents...)
		}
		candidates = append(candidates, ws.LatestBuild.Agents...)
	}
	for _, r := range ws.Resources {
		candidates = append(candidates, r.Agents...)
	}
	candidates = append(candidates, ws.Agents...)

	var unique []WorkspaceAgent
	seen := make(map[string]struct{})
	for _, a := range candidates {
		key := a.ID
		if key == "" {
			key = a.Name + ":" + a.Status
		}
		if _, ok := seen[key]; !ok {
			seen[key] = struct{}{}
			unique = append(unique, a)
		}
	}
	return unique
}

// WorkspaceHealer manages workspace divergence assessment and restart reconciliation.
type WorkspaceHealer struct {
	Client                    *CoderClient
	ConsecutiveMissThreshold  int
	DryRun                    bool
	FailedMissThreshold       int
	MaxHealsPerRun            int
	OnDecision                func(Decision)
	StateTracker              *StateTracker
}

func (h *WorkspaceHealer) logDecision(workspaceID, owner string, consecutiveMisses int, action, reason string) Decision {
	decision := Decision{
		Action:            action,
		ConsecutiveMisses: consecutiveMisses,
		Owner:             owner,
		Reason:            reason,
		WorkspaceID:       workspaceID,
	}
	if h.OnDecision != nil {
		h.OnDecision(decision)
	} else {
		data, _ := json.Marshal(decision)
		fmt.Println(string(data))
	}
	return decision
}

// EvaluateWorkspace assesses workspace health and triggers or defers healing.
func (h *WorkspaceHealer) EvaluateWorkspace(ctx context.Context, ws *Workspace, healsSoFar int, now float64) (Decision, bool) {
	owner := getWorkspaceOwner(ws)
	if ws.ID == "" {
		return h.logDecision("", owner, 0, "skip", "Workspace missing identifier"), false
	}

	if IsOptedOut(ws) {
		h.StateTracker.RecordHealthy(ws.ID)
		return h.logDecision(ws.ID, owner, 0, "skip", fmt.Sprintf("Workspace opted out via %s", optOutAnnotation)), false
	}

	if ws.LatestBuild == nil {
		h.StateTracker.RecordHealthy(ws.ID)
		return h.logDecision(ws.ID, owner, 0, "skip", "Workspace has no latest build"), false
	}

	if ws.LatestBuild.Transition != "start" {
		h.StateTracker.RecordHealthy(ws.ID)
		return h.logDecision(ws.ID, owner, 0, "skip", fmt.Sprintf("Latest build transition is '%s' (not start)", ws.LatestBuild.Transition)), false
	}

	if _, isTerminal := terminalStatuses[ws.LatestBuild.Status]; !isTerminal {
		return h.logDecision(
			ws.ID,
			owner,
			h.StateTracker.GetMisses(ws.ID),
			"skip",
			fmt.Sprintf("Latest build status is '%s' (in progress/non-terminal)", ws.LatestBuild.Status),
		), false
	}

	var healReason string
	if ws.LatestBuild.Status == "running" {
		agents := getWorkspaceAgents(ws)
		if len(agents) == 0 {
			h.StateTracker.RecordHealthy(ws.ID)
			return h.logDecision(ws.ID, owner, 0, "skip", "Running workspace has no agents"), false
		}
		allUnhealthy := true
		for _, agent := range agents {
			st := strings.ToLower(strings.TrimSpace(agent.Status))
			if _, ok := unhealthyAgentStatuses[st]; !ok {
				allUnhealthy = false
				break
			}
		}
		if !allUnhealthy {
			h.StateTracker.RecordHealthy(ws.ID)
			return h.logDecision(ws.ID, owner, 0, "skip", "Workspace is healthy (agents connected or connecting)"), false
		}
		misses := h.StateTracker.RecordMiss(ws.ID, now)
		if misses < h.ConsecutiveMissThreshold {
			return h.logDecision(
				ws.ID,
				owner,
				misses,
				"observe",
				fmt.Sprintf("All agents disconnected/timed out: consecutive misses (%d) < threshold (%d)", misses, h.ConsecutiveMissThreshold),
			), false
		}
		healReason = fmt.Sprintf(
			"Running workspace with all agents disconnected/timed out (consecutive misses: %d >= %d)",
			misses, h.ConsecutiveMissThreshold,
		)
	} else { // failed
		misses := h.StateTracker.RecordMiss(ws.ID, now)
		if misses < h.FailedMissThreshold {
			return h.logDecision(
				ws.ID,
				owner,
				misses,
				"observe",
				fmt.Sprintf("Start build failed: consecutive misses (%d) < threshold (%d)", misses, h.FailedMissThreshold),
			), false
		}
		healReason = fmt.Sprintf(
			"Start build failed after reaper timeout or error (consecutive misses: %d >= %d)",
			misses, h.FailedMissThreshold,
		)
	}

	currentMisses := h.StateTracker.GetMisses(ws.ID)
	if healsSoFar >= h.MaxHealsPerRun {
		return h.logDecision(
			ws.ID,
			owner,
			currentMisses,
			"defer",
			fmt.Sprintf("Max heals limit reached (%d/%d)", healsSoFar, h.MaxHealsPerRun),
		), false
	}

	canHeal, deferReason := h.StateTracker.CanHealWithReason(ws.ID, now)
	if !canHeal {
		return h.logDecision(ws.ID, owner, currentMisses, "defer", deferReason), false
	}

	if h.DryRun {
		return h.logDecision(ws.ID, owner, currentMisses, "dry_run_restart", healReason), true
	}

	if err := h.Client.RestartWorkspace(ctx, ws.ID); err != nil {
		return h.logDecision(ws.ID, owner, currentMisses, "error", fmt.Sprintf("Failed to restart workspace: %v", err)), false
	}

	h.StateTracker.RecordHealthy(ws.ID)
	h.StateTracker.RecordHeal(ws.ID, now)
	return h.logDecision(ws.ID, owner, currentMisses, "restart", healReason), true
}

// HealWorkspaces runs one reconciliation cycle across all active workspaces.
func (h *WorkspaceHealer) HealWorkspaces(ctx context.Context) ([]Decision, error) {
	workspaces, err := h.Client.ListWorkspaces(ctx)
	if err != nil {
		return nil, err
	}

	activeIDs := make(map[string]struct{}, len(workspaces))
	for _, w := range workspaces {
		if w.ID != "" {
			activeIDs[w.ID] = struct{}{}
		}
	}
	h.StateTracker.Prune(activeIDs)

	now := float64(time.Now().UnixNano()) / 1e9
	var decisions []Decision
	healsCount := 0
	for i := range workspaces {
		dec, didHeal := h.EvaluateWorkspace(ctx, &workspaces[i], healsCount, now)
		decisions = append(decisions, dec)
		if didHeal {
			healsCount++
		}
	}

	if !h.DryRun {
		_ = h.StateTracker.Save(ctx, now)
	}
	return decisions, nil
}

func touchHeartbeat(path string) error {
	if path == "" {
		return nil
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	now := time.Now()
	if err := os.Chtimes(path, now, now); err != nil {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			return err
		}
		return f.Close()
	}
	return nil
}

func runContinuousLoop(ctx context.Context, healer *WorkspaceHealer, intervalSeconds int, heartbeatFile string) error {
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGTERM, syscall.SIGINT)

	if intervalSeconds <= 0 {
		intervalSeconds = 120
	}
	ticker := time.NewTicker(time.Duration(intervalSeconds) * time.Second)
	defer ticker.Stop()

	runCycle := func() {
		healer.StateTracker.ResetRunCounters()
		if _, err := healer.HealWorkspaces(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "Error executing workspace healer cycle: %v\n", err)
		} else {
			_ = touchHeartbeat(heartbeatFile)
		}
	}

	runCycle()

	for {
		select {
		case <-sigChan:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			runCycle()
		}
	}
}

func checkHeartbeat(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func handleBootstrapToken(ctx context.Context, coderURL, tokenFile, fallbackTokenFile string) error {
	if fallbackTokenFile != "" {
		if token, err := readTokenSafely(fallbackTokenFile, false); err == nil && token != "" {
			dest := tokenFile
			if dest == "" {
				dest = os.Getenv("CODER_SESSION_TOKEN_FILE")
			}
			if dest != "" {
				return writeTokenSafely(dest, token)
			}
			return nil
		}
	}

	bootstrapURL := os.Getenv("CODER_BOOTSTRAP_URL")
	issuerURL := os.Getenv("CODER_BOOTSTRAP_OIDC_ISSUER")
	user := os.Getenv("CODER_BOOTSTRAP_OIDC_USERNAME")
	pass := os.Getenv("CODER_BOOTSTRAP_OIDC_PASSWORD")
	allowInsecure := os.Getenv("CODER_BOOTSTRAP_ALLOW_INSECURE") == "true"
	targetCoderURL := coderURL
	if targetCoderURL == "" {
		targetCoderURL = bootstrapURL
	}

	sessionToken, err := BrowserLogin(ctx, bootstrapURL, issuerURL, user, pass, allowInsecure)
	if err != nil {
		return err
	}

	tempClient := NewCoderClient(targetCoderURL, sessionToken, nil)
	scopedToken, err := MintScopedToken(ctx, tempClient, user)
	_ = Logout(ctx, tempClient)
	if err != nil {
		return err
	}

	dest := tokenFile
	if dest == "" {
		dest = os.Getenv("CODER_SESSION_TOKEN_FILE")
	}
	if dest != "" {
		return writeTokenSafely(dest, scopedToken)
	}
	return nil
}

func resolveSessionToken(tokenFlag, tokenFile string, unlinkToken bool) (string, error) {
	if tokenFlag != "" {
		return tokenFlag, nil
	}
	if tokenFile != "" {
		return readTokenSafely(tokenFile, unlinkToken)
	}
	for _, candidate := range []string{
		"/var/run/coder/session-token/coder-session-token",
		"/state/coder-session-token",
	} {
		if t, err := readTokenSafely(candidate, false); err == nil && t != "" {
			return t, nil
		}
	}
	return "", errors.New("--token-file, CODER_SESSION_TOKEN_FILE, or CODER_SESSION_TOKEN is required")
}

func resolveBackend(stateBackendFlag, stateFile, stateConfigMap, namespace string) StateBackend {
	switch stateBackendFlag {
	case "file":
		return &FileBackend{Path: stateFile}
	case "memory":
		return NewInMemoryBackend()
	case "configmap":
		return NewConfigMapBackend(stateConfigMap, namespace, defaultDataKey, nil)
	default:
		if stateFile != "" {
			return &FileBackend{Path: stateFile}
		}
		if os.Getenv("KUBERNETES_SERVICE_HOST") != "" || isFileAccessible("/var/run/secrets/kubernetes.io/serviceaccount/token") || isFileAccessible("/var/run/coder/oauth-bootstrap/token") {
			return NewConfigMapBackend(stateConfigMap, namespace, defaultDataKey, nil)
		}
		return NewInMemoryBackend()
	}
}

func main() {
	var (
		coderURL                 = flag.String("coder-url", os.Getenv("CODER_URL"), "Coder API base URL")
		tokenFile                = flag.String("token-file", os.Getenv("CODER_SESSION_TOKEN_FILE"), "Session token file path")
		tokenFlag                = flag.String("token", os.Getenv("CODER_SESSION_TOKEN"), "Coder session token directly")
		unlinkToken              = flag.Bool("unlink-token", false, "Unlink token file after reading")
		dryRun                   = flag.Bool("dry-run", false, "Log healing decisions without mutating workspaces")
		maxHealsPerRun           = flag.Int("max-heals-per-run", 3, "Maximum workspaces to heal per run")
		consecutiveMissThreshold = flag.Int("consecutive-miss-threshold", 3, "Misses required before healing running workspaces")
		failedMissThreshold      = flag.Int("failed-miss-threshold", 1, "Misses required before healing failed builds")
		stateFile                = flag.String("state-file", os.Getenv("CODER_HEALER_STATE_FILE"), "Local JSON state file path")
		stateBackendFlag         = flag.String("state-backend", "", "State persistence backend (configmap, file, memory)")
		stateConfigMap           = flag.String("state-configmap", defaultConfigMapName, "ConfigMap name for state storage")
		namespace                = flag.String("namespace", defaultNamespace, "Kubernetes namespace")
		rateLimit                = flag.Float64("rate-limit", 5.0, "Coder API request rate limit (requests per second)")
		continuous               = flag.Bool("continuous", false, "Run continuous reconciliation loop")
		intervalSeconds          = flag.Int("interval-seconds", 120, "Reconciliation cycle interval in seconds")
		heartbeatFile            = flag.String("heartbeat-file", os.Getenv("CODER_HEALER_HEARTBEAT_FILE"), "Heartbeat file path")
		checkHeartbeatPath       = flag.String("check-heartbeat", "", "Check heartbeat file existence for liveness probe")
		bootstrapOnly            = flag.Bool("bootstrap-token-only", false, "Bootstrap scoped session token and exit")
		fallbackTokenFile        = flag.String("session-token-fallback", "", "Optional pre-existing token file to copy")
	)
	flag.Parse()

	if *checkHeartbeatPath != "" {
		if !checkHeartbeat(*checkHeartbeatPath) {
			os.Exit(1)
		}
		os.Exit(0)
	}

	ctx := context.Background()

	if *bootstrapOnly {
		if err := handleBootstrapToken(ctx, *coderURL, *tokenFile, *fallbackTokenFile); err != nil {
			fmt.Fprintf(os.Stderr, "Coder workspace healer bootstrap failed: %v\n", err)
			os.Exit(1)
		}
		os.Exit(0)
	}

	if *coderURL == "" {
		fmt.Fprintln(os.Stderr, "Error: --coder-url or CODER_URL environment variable is required")
		os.Exit(1)
	}

	sessionToken, err := resolveSessionToken(*tokenFlag, *tokenFile, *unlinkToken)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}

	backend := resolveBackend(*stateBackendFlag, *stateFile, *stateConfigMap, *namespace)
	tracker := NewStateTracker(backend, defaultMaxHealsPerWindow, defaultRollingWindowSeconds, *maxHealsPerRun)
	_ = tracker.Load(ctx)

	limiter := NewRateLimiter(*rateLimit)
	client := NewCoderClient(*coderURL, sessionToken, limiter)

	healer := &WorkspaceHealer{
		Client:                   client,
		ConsecutiveMissThreshold: *consecutiveMissThreshold,
		DryRun:                   *dryRun,
		FailedMissThreshold:      *failedMissThreshold,
		MaxHealsPerRun:           *maxHealsPerRun,
		StateTracker:             tracker,
	}

	if *continuous {
		if err := runContinuousLoop(ctx, healer, *intervalSeconds, *heartbeatFile); err != nil {
			fmt.Fprintf(os.Stderr, "Continuous loop terminated with error: %v\n", err)
			os.Exit(1)
		}
		os.Exit(0)
	}

	if _, err := healer.HealWorkspaces(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "Error executing workspace healer: %v\n", err)
		os.Exit(1)
	}
	_ = touchHeartbeat(*heartbeatFile)
}

func isFileAccessible(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}
