package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func makeTestWorkspace(
	id string,
	ownerName string,
	status string,
	transition string,
	agentStatuses []string,
	annotations map[string]interface{},
	labels map[string]interface{},
	jobError string,
) Workspace {
	var agents []WorkspaceAgent
	for i, st := range agentStatuses {
		agents = append(agents, WorkspaceAgent{
			ID:     fmt.Sprintf("agent-%d", i),
			Name:   fmt.Sprintf("agent-%d", i),
			Status: st,
		})
	}
	buildStatus := "succeeded"
	if status == "failed" {
		buildStatus = "failed"
	}

	build := &WorkspaceBuild{
		ID:         "build-1",
		Transition: transition,
		Status:     status,
		Job: WorkspaceJob{
			Error:  jobError,
			Status: buildStatus,
		},
		Resources: []WorkspaceResource{
			{
				Name:   "compute",
				Agents: agents,
			},
		},
	}

	return Workspace{
		ID:          id,
		Name:        "name-" + id,
		OwnerName:   ownerName,
		LatestBuild: build,
		Annotations: annotations,
		Labels:      labels,
	}
}

type fakeHTTPClient struct {
	mu        sync.Mutex
	calls     []*http.Request
	responses map[string]*http.Response
	handler   func(req *http.Request) (*http.Response, error)
}

func (f *fakeHTTPClient) Do(req *http.Request) (*http.Response, error) {
	f.mu.Lock()
	f.calls = append(f.calls, req)
	f.mu.Unlock()

	if f.handler != nil {
		return f.handler(req)
	}
	key := req.Method + " " + req.URL.Path
	if resp, ok := f.responses[key]; ok {
		return resp, nil
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader("{}")),
		Header:     make(http.Header),
	}, nil
}

// ---------------------- Safe Token Reading Tests ----------------------

func TestSafeTokenReading(t *testing.T) {
	tmpDir := t.TempDir()

	t.Run("resolves_symlinks", func(t *testing.T) {
		realFile := filepath.Join(tmpDir, "real_token")
		if err := os.WriteFile(realFile, []byte("secret-session-token\n"), 0600); err != nil {
			t.Fatal(err)
		}
		symlink := filepath.Join(tmpDir, "token_symlink")
		if err := os.Symlink(realFile, symlink); err != nil {
			t.Fatal(err)
		}

		token, err := readTokenSafely(symlink, false)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if token != "secret-session-token" {
			t.Fatalf("expected 'secret-session-token', got '%s'", token)
		}
	})

	t.Run("fails_when_symlink_is_broken", func(t *testing.T) {
		symlink := filepath.Join(tmpDir, "broken_symlink")
		_ = os.Symlink(filepath.Join(tmpDir, "nonexistent"), symlink)

		_, err := readTokenSafely(symlink, false)
		if err == nil {
			t.Fatal("expected error for broken symlink")
		}
	})

	t.Run("fails_when_path_is_directory", func(t *testing.T) {
		dir := filepath.Join(tmpDir, "dir_token")
		_ = os.Mkdir(dir, 0755)

		_, err := readTokenSafely(dir, false)
		if err == nil {
			t.Fatal("expected error when path is directory")
		}
	})

	t.Run("fails_when_file_is_empty", func(t *testing.T) {
		emptyFile := filepath.Join(tmpDir, "empty_token")
		_ = os.WriteFile(emptyFile, []byte("   \n"), 0600)

		_, err := readTokenSafely(emptyFile, false)
		if err == nil {
			t.Fatal("expected error when token file is empty")
		}
	})

	t.Run("succeeds_and_unlinks_when_requested", func(t *testing.T) {
		file := filepath.Join(tmpDir, "unlink_token")
		_ = os.WriteFile(file, []byte("valid-coder-token\n"), 0600)

		token, err := readTokenSafely(file, true)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if token != "valid-coder-token" {
			t.Fatalf("unexpected token: %s", token)
		}
		if _, err := os.Stat(file); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("expected token file to be unlinked")
		}
	})
}

// ---------------------- Opt-Out Annotation Tests ----------------------

func TestOptOutRules(t *testing.T) {
	t.Run("returns_true_for_workspace_annotation", func(t *testing.T) {
		ws := makeTestWorkspace("ws-1", "alice", "running", "start", nil, map[string]interface{}{optOutAnnotation: "true"}, nil, "")
		if !IsOptedOut(&ws) {
			t.Fatal("expected opted out via annotation")
		}
	})

	t.Run("returns_true_for_workspace_label", func(t *testing.T) {
		ws := makeTestWorkspace("ws-1", "alice", "running", "start", nil, nil, map[string]interface{}{"healer.coder.openplex.dev/opt-out": "yes"}, "")
		if !IsOptedOut(&ws) {
			t.Fatal("expected opted out via label")
		}
	})

	t.Run("returns_true_for_resource_metadata", func(t *testing.T) {
		ws := makeTestWorkspace("ws-1", "alice", "running", "start", nil, nil, nil, "")
		ws.LatestBuild.Resources = []WorkspaceResource{
			{
				Metadata: []WorkspaceParam{
					{Key: optOutAnnotation, Value: "1"},
				},
			},
		}
		if !IsOptedOut(&ws) {
			t.Fatal("expected opted out via resource metadata")
		}
	})

	t.Run("returns_true_for_parameters", func(t *testing.T) {
		ws := makeTestWorkspace("ws-1", "alice", "running", "start", nil, nil, nil, "")
		ws.Parameters = []WorkspaceParam{
			{Name: "custom-opt-out-healer", Value: "enabled"},
		}
		if !IsOptedOut(&ws) {
			t.Fatal("expected opted out via parameter")
		}
	})

	t.Run("returns_false_when_unannotated", func(t *testing.T) {
		ws := makeTestWorkspace("ws-1", "alice", "running", "start", nil, nil, nil, "")
		if IsOptedOut(&ws) {
			t.Fatal("expected not opted out")
		}
	})
}

// ---------------------- State Tracker Tests ----------------------

func TestStateTracker(t *testing.T) {
	t.Run("increments_misses_and_resets_healthy", func(t *testing.T) {
		tracker := NewStateTracker(nil, 2, 3600, 5)
		if tracker.GetMissCount("ws-1") != 0 {
			t.Fatalf("expected 0 misses, got %d", tracker.GetMissCount("ws-1"))
		}

		m1 := tracker.RecordMiss("ws-1", 100.0)
		m2 := tracker.RecordMiss("ws-1", 120.0)
		if m1 != 1 || m2 != 2 {
			t.Fatalf("expected 1 then 2 misses, got %d then %d", m1, m2)
		}
		if tracker.GetMissCount("ws-1") != 2 {
			t.Fatalf("expected 2 misses, got %d", tracker.GetMissCount("ws-1"))
		}
		first, last := tracker.GetMissTimestamps("ws-1")
		if first == nil || *first != 100.0 || last == nil || *last != 120.0 {
			t.Fatalf("timestamps mismatch: %v, %v", first, last)
		}

		tracker.RecordHealthy("ws-1")
		if tracker.GetMissCount("ws-1") != 0 {
			t.Fatalf("expected 0 misses after healthy, got %d", tracker.GetMissCount("ws-1"))
		}
		first, last = tracker.GetMissTimestamps("ws-1")
		if first != nil || last != nil {
			t.Fatal("expected nil timestamps after healthy")
		}
	})

	t.Run("prunes_inactive_workspaces_preserving_heal_history", func(t *testing.T) {
		tracker := NewStateTracker(nil, 2, 3600, 5)
		tracker.RecordMiss("ws-deleted", 100.0)
		tracker.RecordMiss("ws-active", 100.0)
		tracker.RecordHeal("ws-with-heals", 100.0)

		pruned := tracker.PruneInactiveWorkspaces(map[string]struct{}{"ws-active": {}})
		if pruned != 1 {
			t.Fatalf("expected 1 pruned, got %d", pruned)
		}
		if tracker.GetMissCount("ws-deleted") != 0 {
			t.Fatal("expected ws-deleted to be pruned")
		}
		if tracker.GetMissCount("ws-active") != 1 {
			t.Fatal("expected ws-active to be preserved")
		}
		if len(tracker.GetHealHistory("ws-with-heals")) != 1 {
			t.Fatal("expected ws-with-heals heal history to be preserved")
		}
	})

	t.Run("can_heal_rejects_when_workspace_rolling_window_limit_reached", func(t *testing.T) {
		tracker := NewStateTracker(nil, 2, 6*3600.0, 10)
		baseTime := 1000.0

		if !tracker.CanHeal("ws-1", baseTime) {
			t.Fatal("expected can heal initially")
		}

		tracker.RecordHeal("ws-1", baseTime)
		if !tracker.CanHeal("ws-1", baseTime+3600.0) {
			t.Fatal("expected can heal second time")
		}

		tracker.RecordHeal("ws-1", baseTime+3600.0)
		canHeal, reason := tracker.CanHealWithReason("ws-1", baseTime+7200.0)
		if canHeal {
			t.Fatal("expected cannot heal when rate limit reached")
		}
		if !strings.Contains(reason, "workspace heal rate limit reached") {
			t.Fatalf("unexpected reason: %s", reason)
		}

		if !tracker.CanHeal("ws-2", baseTime+7200.0) {
			t.Fatal("expected ws-2 can heal")
		}
	})

	t.Run("can_heal_allows_heal_after_rolling_window_elapses", func(t *testing.T) {
		window := 6 * 3600.0
		tracker := NewStateTracker(nil, 2, window, 10)
		t0 := 1000.0
		tracker.RecordHeal("ws-1", t0)
		tracker.RecordHeal("ws-1", t0+3600.0)

		if !tracker.CanHeal("ws-1", t0+window+1.0) {
			t.Fatal("expected can heal after window elapsed")
		}
	})

	t.Run("can_heal_rejects_when_fleet_concurrency_limit_reached", func(t *testing.T) {
		tracker := NewStateTracker(nil, 5, 6*3600.0, 2)
		t0 := 5000.0

		if !tracker.CanHeal("ws-1", t0) {
			t.Fatal("ws-1 should be permitted")
		}
		tracker.RecordHeal("ws-1", t0)
		if tracker.CurrentRunHeals() != 1 {
			t.Fatalf("expected 1 run heal, got %d", tracker.CurrentRunHeals())
		}

		if !tracker.CanHeal("ws-2", t0) {
			t.Fatal("ws-2 should be permitted")
		}
		tracker.RecordHeal("ws-2", t0)
		if tracker.CurrentRunHeals() != 2 {
			t.Fatalf("expected 2 run heals, got %d", tracker.CurrentRunHeals())
		}

		canHeal, reason := tracker.CanHealWithReason("ws-3", t0)
		if canHeal {
			t.Fatal("ws-3 should be rejected")
		}
		if !strings.Contains(reason, "fleet concurrency limit reached") {
			t.Fatalf("unexpected reason: %s", reason)
		}

		tracker.ResetRunCounters()
		if tracker.CurrentRunHeals() != 0 {
			t.Fatal("expected 0 run heals after reset")
		}
		if !tracker.CanHeal("ws-3", t0) {
			t.Fatal("ws-3 should be permitted after reset")
		}
	})

	t.Run("prune_heal_history_removes_expired_timestamps", func(t *testing.T) {
		tracker := NewStateTracker(nil, 5, 3600.0, 10)
		tracker.RecordHeal("ws-1", 100.0)
		tracker.RecordHeal("ws-1", 2000.0)
		tracker.RecordHeal("ws-1", 4500.0)

		pruned := tracker.PruneHealHistory(3600.0, 5000.0)
		if pruned != 1 {
			t.Fatalf("expected 1 pruned, got %d", pruned)
		}
		hist := tracker.GetHealHistory("ws-1")
		if len(hist) != 2 || hist[0] != 2000.0 || hist[1] != 4500.0 {
			t.Fatalf("unexpected history: %v", hist)
		}
	})

	t.Run("in_memory_backend_persistence_roundtrip", func(t *testing.T) {
		backend := NewInMemoryBackend()
		ctx := context.Background()

		tracker1 := NewStateTracker(backend, 2, 3600, 5)
		tracker1.RecordMiss("ws-a", 100.0)
		tracker1.RecordHeal("ws-a", 150.0)
		if err := tracker1.Save(ctx, 200.0); err != nil {
			t.Fatal(err)
		}

		tracker2 := NewStateTracker(backend, 2, 3600, 5)
		if err := tracker2.Load(ctx); err != nil {
			t.Fatal(err)
		}

		if tracker2.GetMissCount("ws-a") != 1 {
			t.Fatalf("expected 1 miss, got %d", tracker2.GetMissCount("ws-a"))
		}
		if hist := tracker2.GetHealHistory("ws-a"); len(hist) != 1 || hist[0] != 150.0 {
			t.Fatalf("unexpected history: %v", hist)
		}
		first, _ := tracker2.GetMissTimestamps("ws-a")
		if first == nil || *first != 100.0 {
			t.Fatalf("unexpected first miss timestamp: %v", first)
		}
	})

	t.Run("file_backend_saves_and_loads_valid_json", func(t *testing.T) {
		tmpDir := t.TempDir()
		filePath := filepath.Join(tmpDir, "state.json")
		backend := &FileBackend{Path: filePath}
		ctx := context.Background()

		tracker1 := NewStateTracker(backend, 2, 3600, 5)
		tracker1.RecordMiss("ws-disk", 300.0)
		tracker1.RecordHeal("ws-disk", 350.0)
		if err := tracker1.Save(ctx, 400.0); err != nil {
			t.Fatal(err)
		}

		content, err := os.ReadFile(filePath)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(content), "ws-disk") {
			t.Fatalf("expected file to contain ws-disk: %s", string(content))
		}

		tracker2 := NewStateTracker(backend, 2, 3600, 5)
		if err := tracker2.Load(ctx); err != nil {
			t.Fatal(err)
		}
		if tracker2.GetMissCount("ws-disk") != 1 {
			t.Fatalf("expected 1 miss, got %d", tracker2.GetMissCount("ws-disk"))
		}
	})

	t.Run("file_backend_rejects_corrupted_json", func(t *testing.T) {
		tmpDir := t.TempDir()
		filePath := filepath.Join(tmpDir, "corrupted.json")
		_ = os.WriteFile(filePath, []byte("{broken json"), 0644)

		backend := &FileBackend{Path: filePath}
		_, err := backend.Load(context.Background())
		if err == nil {
			t.Fatal("expected error on corrupted json")
		}
	})

	t.Run("configmap_backend_creates_and_patches_configmap", func(t *testing.T) {
		ctx := context.Background()
		resources := make(map[string]string)
		var mu sync.Mutex

		fakeClient := &fakeHTTPClient{
			handler: func(req *http.Request) (*http.Response, error) {
				mu.Lock()
				defer mu.Unlock()
				path := req.URL.Path
				if req.Method == http.MethodGet {
					val, ok := resources[path]
					if !ok {
						return &http.Response{StatusCode: http.StatusNotFound, Body: io.NopCloser(strings.NewReader("{}"))}, nil
					}
					return &http.Response{
						StatusCode: http.StatusOK,
						Body:       io.NopCloser(strings.NewReader(val)),
					}, nil
				}
				if req.Method == http.MethodPatch {
					body, _ := io.ReadAll(req.Body)
					var patch map[string]interface{}
					_ = json.Unmarshal(body, &patch)
					if _, ok := resources[path]; !ok {
						return &http.Response{StatusCode: http.StatusNotFound, Body: io.NopCloser(strings.NewReader("{}"))}, nil
					}
					var existing map[string]interface{}
					_ = json.Unmarshal([]byte(resources[path]), &existing)
					dataMap, _ := patch["data"].(map[string]interface{})
					existingData, _ := existing["data"].(map[string]interface{})
					if existingData == nil {
						existingData = make(map[string]interface{})
					}
					for k, v := range dataMap {
						existingData[k] = v
					}
					existing["data"] = existingData
					merged, _ := json.Marshal(existing)
					resources[path] = string(merged)
					return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(bytes.NewReader(merged))}, nil
				}
				if req.Method == http.MethodPost {
					body, _ := io.ReadAll(req.Body)
					var created map[string]interface{}
					_ = json.Unmarshal(body, &created)
					meta, _ := created["metadata"].(map[string]interface{})
					name, _ := meta["name"].(string)
					targetPath := fmt.Sprintf("%s/%s", strings.TrimRight(path, "/"), name)
					resources[targetPath] = string(body)
					return &http.Response{StatusCode: http.StatusCreated, Body: io.NopCloser(bytes.NewReader(body))}, nil
				}
				return &http.Response{StatusCode: http.StatusBadRequest, Body: io.NopCloser(strings.NewReader("{}"))}, nil
			},
		}

		backend := NewConfigMapBackend("test-cm", "coder", "state.json", fakeClient)

		// Initial load returns nil
		loaded, err := backend.Load(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if loaded != nil {
			t.Fatalf("expected nil on empty CM, got %v", loaded)
		}

		// First save creates
		firstState := map[string]*WorkspaceState{
			"ws-1": {MissCount: 3},
		}
		if err := backend.Save(ctx, firstState, 100.0); err != nil {
			t.Fatal(err)
		}

		// Load verified
		loaded, err = backend.Load(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if loaded == nil || loaded["ws-1"] == nil || loaded["ws-1"].MissCount != 3 {
			t.Fatalf("unexpected loaded state: %v", loaded)
		}

		// Second save updates
		secondState := map[string]*WorkspaceState{
			"ws-updated": {MissCount: 5, HealHistory: []float64{10.0}},
		}
		if err := backend.Save(ctx, secondState, 200.0); err != nil {
			t.Fatal(err)
		}
		loaded, err = backend.Load(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if loaded == nil || loaded["ws-updated"] == nil || loaded["ws-updated"].MissCount != 5 {
			t.Fatalf("unexpected updated loaded state: %v", loaded)
		}
	})

	t.Run("to_json_and_from_json_serialization_roundtrip", func(t *testing.T) {
		tracker1 := NewStateTracker(nil, 2, 3600, 5)
		tracker1.RecordMiss("ws-json", 100.0)
		tracker1.RecordHeal("ws-json", 200.0)

		jsonStr, err := tracker1.ToJSON(300.0)
		if err != nil {
			t.Fatal(err)
		}

		tracker2 := NewStateTracker(nil, 2, 3600, 5)
		if err := tracker2.FromJSON(jsonStr); err != nil {
			t.Fatal(err)
		}

		if tracker2.GetMissCount("ws-json") != 1 {
			t.Fatalf("expected 1 miss, got %d", tracker2.GetMissCount("ws-json"))
		}
		if hist := tracker2.GetHealHistory("ws-json"); len(hist) != 1 || hist[0] != 200.0 {
			t.Fatalf("unexpected history: %v", hist)
		}
	})

	t.Run("evaluate_healing_rate_limits_pure_function", func(t *testing.T) {
		now := 10000.0
		window := 3600.0

		can, reason := evaluateHealingRateLimits(nil, 3, 3, 2, window, now)
		if can || !strings.Contains(reason, "fleet concurrency limit reached") {
			t.Fatalf("expected fleet limit rejection, got %v (%s)", can, reason)
		}

		can, reason = evaluateHealingRateLimits([]float64{now - 100, now - 50}, 0, 5, 2, window, now)
		if can || !strings.Contains(reason, "workspace heal rate limit reached") {
			t.Fatalf("expected workspace limit rejection, got %v (%s)", can, reason)
		}

		can, reason = evaluateHealingRateLimits([]float64{now - 4000}, 0, 5, 2, window, now)
		if !can || reason != "permitted" {
			t.Fatalf("expected permitted, got %v (%s)", can, reason)
		}
	})
}

// ---------------------- Workspace Healer Decision Tests ----------------------

func TestWorkspaceHealerDecisions(t *testing.T) {
	ctx := context.Background()

	setupTestHealer := func(workspaces []Workspace, restartHandler func(workspaceID string) error) (*WorkspaceHealer, *fakeHTTPClient) {
		tracker := NewStateTracker(nil, 3, 3600, 3)
		fakeClient := &fakeHTTPClient{
			handler: func(req *http.Request) (*http.Response, error) {
				if req.Method == http.MethodGet && req.URL.Path == "/api/v2/workspaces" {
					body, _ := json.Marshal(workspaces)
					return &http.Response{
						StatusCode: http.StatusOK,
						Body:       io.NopCloser(bytes.NewReader(body)),
					}, nil
				}
				if req.Method == http.MethodPost && strings.Contains(req.URL.Path, "/builds") {
					parts := strings.Split(req.URL.Path, "/")
					wsID := parts[4]
					if restartHandler != nil {
						if err := restartHandler(wsID); err != nil {
							return &http.Response{StatusCode: http.StatusInternalServerError, Body: io.NopCloser(strings.NewReader("{}"))}, nil
						}
					}
					return &http.Response{StatusCode: http.StatusCreated, Body: io.NopCloser(strings.NewReader("{}"))}, nil
				}
				return &http.Response{StatusCode: http.StatusNotFound, Body: io.NopCloser(strings.NewReader("{}"))}, nil
			},
		}

		client := NewCoderClient("https://coder.example.com", "test-token", nil, fakeClient)
		healer := &WorkspaceHealer{
			Client:                   client,
			ConsecutiveMissThreshold: 3,
			FailedMissThreshold:      1,
			MaxHealsPerRun:           3,
			StateTracker:             tracker,
		}
		return healer, fakeClient
	}

	t.Run("skips_when_opt_out_present", func(t *testing.T) {
		ws := makeTestWorkspace("ws-optout", "alice", "running", "start", []string{"disconnected"}, map[string]interface{}{optOutAnnotation: "true"}, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "skip" || decisions[0].WorkspaceID != "ws-optout" {
			t.Fatalf("unexpected decisions: %+v", decisions)
		}
	})

	t.Run("skips_when_transition_is_not_start", func(t *testing.T) {
		ws := makeTestWorkspace("ws-stop", "alice", "running", "stop", []string{"disconnected"}, nil, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "skip" {
			t.Fatalf("unexpected decisions: %+v", decisions)
		}
	})

	t.Run("skips_when_status_in_progress", func(t *testing.T) {
		ws := makeTestWorkspace("ws-starting", "alice", "starting", "start", nil, nil, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "skip" {
			t.Fatalf("unexpected decisions: %+v", decisions)
		}
	})

	t.Run("skips_healthy_running_workspace_with_connected_agents", func(t *testing.T) {
		ws := makeTestWorkspace("ws-healthy", "alice", "running", "start", []string{"connected"}, nil, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "skip" || decisions[0].ConsecutiveMisses != 0 {
			t.Fatalf("unexpected decisions: %+v", decisions)
		}
	})

	t.Run("observes_running_workspace_until_miss_threshold", func(t *testing.T) {
		ws := makeTestWorkspace("ws-zombie", "alice", "running", "start", []string{"disconnected"}, nil, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)

		// Run 1: miss 1 -> observe
		d1, _ := healer.HealWorkspaces(ctx)
		if d1[0].Action != "observe" || d1[0].ConsecutiveMisses != 1 {
			t.Fatalf("run 1 expected observe miss 1, got %+v", d1[0])
		}

		// Run 2: miss 2 -> observe
		d2, _ := healer.HealWorkspaces(ctx)
		if d2[0].Action != "observe" || d2[0].ConsecutiveMisses != 2 {
			t.Fatalf("run 2 expected observe miss 2, got %+v", d2[0])
		}

		// Run 3: miss 3 >= threshold -> restart
		d3, _ := healer.HealWorkspaces(ctx)
		if d3[0].Action != "restart" || d3[0].ConsecutiveMisses != 3 {
			t.Fatalf("run 3 expected restart miss 3, got %+v", d3[0])
		}
	})

	t.Run("restarts_failed_start_build_on_first_miss", func(t *testing.T) {
		ws := makeTestWorkspace("ws-failed", "alice", "failed", "start", nil, nil, nil, "provisioner timeout")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "restart" || decisions[0].WorkspaceID != "ws-failed" {
			t.Fatalf("unexpected decisions: %+v", decisions)
		}
	})

	t.Run("respects_max_heals_per_run_limit", func(t *testing.T) {
		var workspaces []Workspace
		for i := 0; i < 5; i++ {
			workspaces = append(workspaces, makeTestWorkspace(fmt.Sprintf("ws-failed-%d", i), "alice", "failed", "start", nil, nil, nil, ""))
		}
		healer, _ := setupTestHealer(workspaces, nil)

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}

		restarts := 0
		defers := 0
		for _, d := range decisions {
			if d.Action == "restart" {
				restarts++
			} else if d.Action == "defer" {
				defers++
			}
		}
		if restarts != 3 || defers != 2 {
			t.Fatalf("expected 3 restarts and 2 defers, got %d restarts and %d defers", restarts, defers)
		}
	})

	t.Run("dry_run_does_not_mutate_or_call_restart_api", func(t *testing.T) {
		ws := makeTestWorkspace("ws-dry", "alice", "failed", "start", nil, nil, nil, "")
		healer, fakeClient := setupTestHealer([]Workspace{ws}, nil)
		healer.DryRun = true

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "dry_run_restart" {
			t.Fatalf("unexpected decision: %+v", decisions)
		}

		// Verify no POST was called
		for _, call := range fakeClient.calls {
			if call.Method == http.MethodPost {
				t.Fatalf("unexpected POST call during dry run: %s", call.URL.Path)
			}
		}
	})

	t.Run("records_error_decision_on_restart_failure", func(t *testing.T) {
		ws := makeTestWorkspace("ws-err", "alice", "failed", "start", nil, nil, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, func(_ string) error {
			return errors.New("API unavailable")
		})

		decisions, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if len(decisions) != 1 || decisions[0].Action != "error" {
			t.Fatalf("expected error decision, got %+v", decisions)
		}
	})

	t.Run("structured_log_records_contractual_fields", func(t *testing.T) {
		var recorded Decision
		ws := makeTestWorkspace("ws-log-test", "bob", "failed", "start", nil, nil, nil, "")
		healer, _ := setupTestHealer([]Workspace{ws}, nil)
		healer.OnDecision = func(d Decision) {
			recorded = d
		}

		_, err := healer.HealWorkspaces(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if recorded.WorkspaceID != "ws-log-test" || recorded.Owner != "bob" || recorded.Action != "restart" {
			t.Fatalf("unexpected recorded decision: %+v", recorded)
		}
	})
}

// ---------------------- Heartbeat & Liveness Tests ----------------------

func TestHeartbeat(t *testing.T) {
	tmpDir := t.TempDir()
	hbPath := filepath.Join(tmpDir, "sub", "healer-healthy")

	if checkHeartbeat(hbPath) {
		t.Fatal("expected heartbeat to be false before touch")
	}

	if err := touchHeartbeat(hbPath); err != nil {
		t.Fatal(err)
	}

	if !checkHeartbeat(hbPath) {
		t.Fatal("expected heartbeat to exist after touch")
	}
}

// ---------------------- OIDC Bootstrap Tests ----------------------

func TestOIDCBootstrapFlow(t *testing.T) {
	ctx := context.Background()

	t.Run("first_and_repeat_runs_replace_ephemeral_token", func(t *testing.T) {
		var (
			dexServer   *httptest.Server
			coderServer *httptest.Server
			mu          sync.Mutex
			tokenCount  int
			deletedIDs  []string
		)

		dexServer = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/auth/local") {
				w.Header().Set("Content-Type", "text/html")
				_, _ = w.Write([]byte(fmt.Sprintf(
					`<form method="post" action="/auth/local?state=test"><input name="req" value="fixture"></form>`,
				)))
				return
			}
			if r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/auth/local") {
				_ = r.ParseForm()
				if r.FormValue("login") != "operator@example.invalid" || r.FormValue("password") != "public-local-fixture" {
					w.WriteHeader(http.StatusUnauthorized)
					return
				}
				http.Redirect(w, r, coderServer.URL+"/api/v2/users/oidc/callback?code=valid", http.StatusFound)
				return
			}
			w.WriteHeader(http.StatusNotFound)
		}))
		defer dexServer.Close()

		coderServer = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			mu.Lock()
			defer mu.Unlock()

			if r.URL.Path == "/api/v2/users/oidc/callback" {
				if r.URL.Query().Get("code") == "" {
					http.Redirect(w, r, dexServer.URL+"/auth/local", http.StatusFound)
					return
				}
				http.SetCookie(w, &http.Cookie{
					Name:     "coder_session_token",
					Value:    "owner-session-token",
					Path:     "/",
					HttpOnly: true,
				})
				http.Redirect(w, r, "/", http.StatusFound)
				return
			}
			if r.URL.Path == "/" {
				w.WriteHeader(http.StatusOK)
				return
			}
			if r.URL.Path == "/api/v2/users/me" {
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(map[string]interface{}{
					"email":      "operator@example.invalid",
					"login_type": "oidc",
					"roles":      []map[string]string{{"name": "owner"}},
				})
				return
			}
			if r.URL.Path == "/api/v2/users/me/keys/tokens/local-workspace-healer" {
				if tokenCount == 0 {
					w.WriteHeader(http.StatusNotFound)
					return
				}
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(map[string]string{"id": fmt.Sprintf("healer-token-%d", tokenCount)})
				return
			}
			if r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/v2/users/me/keys/") {
				id := strings.TrimPrefix(r.URL.Path, "/api/v2/users/me/keys/")
				deletedIDs = append(deletedIDs, id)
				w.WriteHeader(http.StatusNoContent)
				return
			}
			if r.Method == http.MethodPost && r.URL.Path == "/api/v2/users/me/keys/tokens" {
				tokenCount++
				w.WriteHeader(http.StatusCreated)
				_ = json.NewEncoder(w).Encode(map[string]string{"key": fmt.Sprintf("scoped-token-value-%d", tokenCount)})
				return
			}
			if r.Method == http.MethodPost && r.URL.Path == "/api/v2/users/logout" {
				w.WriteHeader(http.StatusOK)
				return
			}
			w.WriteHeader(http.StatusNotFound)
		}))
		defer coderServer.Close()

		// Run 1
		token1, err := BrowserLogin(ctx, coderServer.URL, dexServer.URL, "operator@example.invalid", "public-local-fixture", true)
		if err != nil {
			t.Fatal(err)
		}
		c1 := NewCoderClient(coderServer.URL, token1, nil)
		scoped1, err := MintScopedToken(ctx, c1, "operator@example.invalid")
		if err != nil {
			t.Fatal(err)
		}
		_ = Logout(ctx, c1)

		if scoped1 != "scoped-token-value-1" {
			t.Fatalf("expected scoped-token-value-1, got %s", scoped1)
		}
		if len(deletedIDs) != 0 {
			t.Fatalf("expected no deletions on first run, got %v", deletedIDs)
		}

		// Run 2 (repeat replaces token)
		token2, err := BrowserLogin(ctx, coderServer.URL, dexServer.URL, "operator@example.invalid", "public-local-fixture", true)
		if err != nil {
			t.Fatal(err)
		}
		c2 := NewCoderClient(coderServer.URL, token2, nil)
		scoped2, err := MintScopedToken(ctx, c2, "operator@example.invalid")
		if err != nil {
			t.Fatal(err)
		}
		_ = Logout(ctx, c2)

		if scoped2 != "scoped-token-value-2" {
			t.Fatalf("expected scoped-token-value-2, got %s", scoped2)
		}
		if len(deletedIDs) != 1 || deletedIDs[0] != "healer-token-1" {
			t.Fatalf("expected healer-token-1 to be deleted, got %v", deletedIDs)
		}
	})

	t.Run("invalid_credentials_fail_before_token_creation", func(t *testing.T) {
		dexServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodGet {
				_, _ = w.Write([]byte(`<form method="post" action="/auth/local"><input name="test" value="1"></form>`))
				return
			}
			w.WriteHeader(http.StatusUnauthorized)
		}))
		defer dexServer.Close()

		coderServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, dexServer.URL+"/auth/local", http.StatusFound)
		}))
		defer coderServer.Close()

		_, err := BrowserLogin(ctx, coderServer.URL, dexServer.URL, "operator@example.invalid", "wrong-password", true)
		if err == nil {
			t.Fatal("expected error with wrong password")
		}
	})

	t.Run("non_owner_identity_fails_closed", func(t *testing.T) {
		coderServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/api/v2/users/me" {
				_ = json.NewEncoder(w).Encode(map[string]interface{}{
					"email":      "operator@example.invalid",
					"login_type": "oidc",
					"roles":      []map[string]string{{"name": "member"}},
				})
				return
			}
			w.WriteHeader(http.StatusNotFound)
		}))
		defer coderServer.Close()

		client := NewCoderClient(coderServer.URL, "session-token", nil)
		err := RequireOwner(ctx, client, "operator@example.invalid")
		if err == nil {
			t.Fatal("expected error for non-owner user")
		}
	})

	t.Run("untrusted_redirect_fails", func(t *testing.T) {
		coderServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "http://127.0.0.1:9999/attack", http.StatusFound)
		}))
		defer coderServer.Close()

		_, err := BrowserLogin(ctx, coderServer.URL, "http://127.0.0.1:8888", "user", "pass", true)
		if err == nil {
			t.Fatal("expected error on off-target redirect")
		}
	})

	t.Run("insecure_browser_endpoints_require_allow_insecure", func(t *testing.T) {
		_, err := BrowserLogin(ctx, "http://coder.local", "http://dex.local", "user", "pass", false)
		if err == nil {
			t.Fatal("expected error when HTTP used without allowInsecure")
		}
	})
}
