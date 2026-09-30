// Tests workspace preauth-key issuance, state persistence, and cleanup against Headscale CLI output.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"slices"
	"strconv"
	"sync"
	"testing"
	"time"
)

type fakePreAuthKeyCLI struct {
	commands      [][]string
	deleteFailure map[uint64]int
	deleted       []uint64
	keys          map[uint64]headscalePreAuthKey
	mu            sync.Mutex
	nextID        uint64
	now           func() time.Time
}

func newFakePreAuthKeyCLI(now func() time.Time) *fakePreAuthKeyCLI {
	return &fakePreAuthKeyCLI{
		deleteFailure: map[uint64]int{},
		keys:          map[uint64]headscalePreAuthKey{},
		nextID:        1,
		now:           now,
	}
}

func (cli *fakePreAuthKeyCLI) run(_ context.Context, arguments ...string) ([]byte, error) {
	cli.mu.Lock()
	defer cli.mu.Unlock()
	cli.commands = append(cli.commands, slices.Clone(arguments))
	switch {
	case slices.Equal(arguments, []string{"users", "list", "--output", "json"}):
		return []byte(`[{"id":17,"name":"ldap","provider_id":"https://dex.example/dex-subject"}]`), nil
	case slices.Equal(arguments, []string{"preauthkeys", "list", "--output", "json"}):
		keys := make([]headscalePreAuthKey, 0, len(cli.keys))
		for _, key := range cli.keys {
			key.Key = fmt.Sprintf("hskey-auth-%012d-***", key.ID)
			keys = append(keys, key)
		}
		slices.SortFunc(keys, func(left, right headscalePreAuthKey) int {
			return int(left.ID) - int(right.ID)
		})
		return json.Marshal(keys)
	case slices.Equal(arguments, []string{
		"preauthkeys", "create", "--user", "17", "--expiration", "10m", "--ephemeral", "--output", "json",
	}):
		now := cli.now().UTC()
		key := headscalePreAuthKey{
			User:       &headscalePreAuthKeyUser{ID: 17},
			ID:         cli.nextID,
			Key:        fmt.Sprintf("hskey-auth-secret-%d", cli.nextID),
			Ephemeral:  true,
			Expiration: testProtobufTimestamp(now.Add(workspacePreAuthKeyLifetime)),
			CreatedAt:  testProtobufTimestamp(now),
		}
		cli.keys[key.ID] = key
		cli.nextID++
		return json.Marshal(key)
	case len(arguments) == 4 && arguments[0] == "preauthkeys" && arguments[1] == "delete" && arguments[2] == "--id":
		id, err := strconv.ParseUint(arguments[3], 10, 64)
		if err != nil || id == 0 {
			return nil, errors.New("invalid delete ID")
		}
		if cli.deleteFailure[id] > 0 {
			cli.deleteFailure[id]--
			return nil, errors.New("injected Headscale delete failure")
		}
		delete(cli.keys, id)
		cli.deleted = append(cli.deleted, id)
		return []byte("Key deleted\n"), nil
	default:
		return nil, fmt.Errorf("unexpected Headscale command: %q", arguments)
	}
}

func testCommandCoordinator(t *testing.T, cli *fakePreAuthKeyCLI) *commandCoordinator {
	t.Helper()
	path := t.TempDir() + "/workspace-preauthkeys.json"
	if err := initializeWorkspacePreAuthKeys(path); err != nil {
		t.Fatal(err)
	}
	return &commandCoordinator{
		binary: "/ko-app/headscale", config: "/etc/headscale/config.yaml",
		keyMu: &sync.Mutex{}, keyPath: path, now: cli.now, run: cli.run,
	}
}

func testEnrollmentIdentity(workspaceID string) identity {
	return identity{
		SubjectID: fixtureUserID, WorkspaceID: workspaceID,
		OIDCIssuer: "https://dex.example", OIDCSubject: "dex-subject", IdPUsername: "ldap",
	}
}

func TestWorkspacePreAuthKeyRetrySupersedesOutstandingKey(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	cli := newFakePreAuthKeyCLI(func() time.Time { return now })
	coordinator := testCommandCoordinator(t, cli)
	id := testEnrollmentIdentity(fixtureWorkspaceID)

	first, err := coordinator.issue(context.Background(), id, request{})
	if err != nil {
		t.Fatal(err)
	}
	second, err := coordinator.issue(context.Background(), id, request{})
	if err != nil {
		t.Fatal(err)
	}
	if first == second || len(cli.keys) != 1 || cli.keys[2].ID != 2 || !slices.Equal(cli.deleted, []uint64{1}) {
		t.Fatalf("retry did not supersede one key: first=%q second=%q keys=%#v deleted=%v", first, second, cli.keys, cli.deleted)
	}
	tracked, err := readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 1 || tracked[0].KeyID != 2 || tracked[0].OwnerID != fixtureUserID ||
		tracked[0].WorkspaceID != fixtureWorkspaceID {
		t.Fatalf("unexpected durable key state: %#v %v", tracked, err)
	}
	data, err := os.ReadFile(coordinator.keyPath)
	if err != nil || bytes.Contains(data, []byte(second)) {
		t.Fatalf("durable state retained raw preauth key: %q %v", data, err)
	}
}

func TestConcurrentWorkspacePreAuthKeyRequestsLeaveOneOutstandingKey(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	cli := newFakePreAuthKeyCLI(func() time.Time { return now })
	coordinator := testCommandCoordinator(t, cli)
	id := testEnrollmentIdentity(fixtureWorkspaceID)
	const requests = 12
	errors := make(chan error, requests)
	var calls sync.WaitGroup
	for range requests {
		calls.Add(1)
		go func() {
			defer calls.Done()
			_, err := coordinator.issue(context.Background(), id, request{})
			errors <- err
		}()
	}
	calls.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	tracked, err := readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 1 || len(cli.keys) != 1 || len(cli.deleted) != requests-1 {
		t.Fatalf("concurrent issuance escaped its bound: tracked=%#v keys=%d deleted=%d error=%v", tracked, len(cli.keys), len(cli.deleted), err)
	}
}

func TestWorkspacePreAuthKeyReconcileDeletesUsedAndExpiredRows(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	clock := now
	cli := newFakePreAuthKeyCLI(func() time.Time { return clock })
	coordinator := testCommandCoordinator(t, cli)
	firstWorkspace := fixtureWorkspaceID
	secondWorkspace := "823e4567-e89b-42d3-a456-426614174000"
	if _, err := coordinator.issue(context.Background(), testEnrollmentIdentity(firstWorkspace), request{}); err != nil {
		t.Fatal(err)
	}
	if _, err := coordinator.issue(context.Background(), testEnrollmentIdentity(secondWorkspace), request{}); err != nil {
		t.Fatal(err)
	}
	cli.mu.Lock()
	used := cli.keys[1]
	used.Used = true
	cli.keys[1] = used
	cli.mu.Unlock()
	if err := coordinator.reconcilePreAuthKeys(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(cli.keys) != 1 || cli.keys[2].ID != 2 || !slices.Contains(cli.deleted, uint64(1)) {
		t.Fatalf("used row was not deleted: keys=%#v deleted=%v", cli.keys, cli.deleted)
	}
	clock = now.Add(workspacePreAuthKeyLifetime + time.Second)
	if err := coordinator.reconcilePreAuthKeys(context.Background()); err != nil {
		t.Fatal(err)
	}
	tracked, err := readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 0 || len(cli.keys) != 0 || !slices.Contains(cli.deleted, uint64(2)) {
		t.Fatalf("expired row was not deleted: tracked=%#v keys=%#v deleted=%v error=%v", tracked, cli.keys, cli.deleted, err)
	}
}

func TestWorkspacePreAuthKeyReconcileForgetsReusedIdentifier(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	cli := newFakePreAuthKeyCLI(func() time.Time { return now })
	coordinator := testCommandCoordinator(t, cli)
	if _, err := coordinator.issue(context.Background(), testEnrollmentIdentity(fixtureWorkspaceID), request{}); err != nil {
		t.Fatal(err)
	}
	cli.mu.Lock()
	reused := cli.keys[1]
	reused.User.ID = 18
	cli.keys[1] = reused
	cli.mu.Unlock()
	if err := coordinator.reconcilePreAuthKeys(context.Background()); err != nil {
		t.Fatal(err)
	}
	tracked, err := readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 0 || len(cli.keys) != 1 || len(cli.deleted) != 0 {
		t.Fatalf("reused identifier was not safely forgotten: tracked=%#v keys=%#v deleted=%v error=%v", tracked, cli.keys, cli.deleted, err)
	}
}

func TestWorkspacePreAuthKeyReconcileRecoversInterruptedCreate(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	cli := newFakePreAuthKeyCLI(func() time.Time { return now })
	coordinator := testCommandCoordinator(t, cli)
	cli.keys[9] = headscalePreAuthKey{
		ID: 9, Key: "router-prefix", Reusable: true,
		Expiration: testProtobufTimestamp(now.Add(time.Hour)), CreatedAt: testProtobufTimestamp(now),
		ACLTags: []string{"tag:subnet-router"},
	}
	cli.keys[10] = headscalePreAuthKey{
		User: &headscalePreAuthKeyUser{ID: 17}, ID: 10, Key: "orphaned-workspace-key", Ephemeral: true,
		Expiration: testProtobufTimestamp(now.Add(workspacePreAuthKeyLifetime)), CreatedAt: testProtobufTimestamp(now),
	}
	if err := writeWorkspacePreAuthKeys(coordinator.keyPath, []workspacePreAuthKey{{
		BaselineIDs: []uint64{9}, OwnerID: fixtureUserID, StartedAt: now,
		UserID: 17, WorkspaceID: fixtureWorkspaceID,
	}}); err != nil {
		t.Fatal(err)
	}
	if err := coordinator.reconcilePreAuthKeys(context.Background()); err != nil {
		t.Fatal(err)
	}
	tracked, err := readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 0 || len(cli.keys) != 1 || cli.keys[9].ID != 9 ||
		!slices.Equal(cli.deleted, []uint64{10}) {
		t.Fatalf("interrupted create cleanup changed the wrong rows: tracked=%#v keys=%#v deleted=%v error=%v", tracked, cli.keys, cli.deleted, err)
	}
}

func TestWorkspacePreAuthKeyDeleteFailureRemainsFailClosedAndRetries(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	cli := newFakePreAuthKeyCLI(func() time.Time { return now })
	coordinator := testCommandCoordinator(t, cli)
	if _, err := coordinator.issue(context.Background(), testEnrollmentIdentity(fixtureWorkspaceID), request{}); err != nil {
		t.Fatal(err)
	}
	cli.mu.Lock()
	used := cli.keys[1]
	used.Used = true
	cli.keys[1] = used
	cli.deleteFailure[1] = 1
	cli.mu.Unlock()
	if err := coordinator.reconcilePreAuthKeys(context.Background()); err == nil {
		t.Fatal("partial Headscale delete failure was accepted")
	}
	tracked, err := readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 1 || tracked[0].KeyID != 1 {
		t.Fatalf("failed deletion lost its durable retry: %#v %v", tracked, err)
	}
	if err := coordinator.reconcilePreAuthKeys(context.Background()); err != nil {
		t.Fatal(err)
	}
	tracked, err = readWorkspacePreAuthKeys(coordinator.keyPath)
	if err != nil || len(tracked) != 0 || len(cli.keys) != 0 {
		t.Fatalf("delete retry did not converge: tracked=%#v keys=%#v error=%v", tracked, cli.keys, err)
	}
}

func TestWorkspacePreAuthKeyInventoryLimitPreventsIssuance(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	cli := newFakePreAuthKeyCLI(func() time.Time { return now })
	coordinator := testCommandCoordinator(t, cli)
	for id := uint64(1); id <= headscalePreAuthKeyInventoryLimit; id++ {
		cli.keys[id] = headscalePreAuthKey{
			ID: id, Key: fmt.Sprintf("router-%d", id), Reusable: true,
			Expiration: testProtobufTimestamp(now.Add(time.Hour)), CreatedAt: testProtobufTimestamp(now),
			ACLTags: []string{"tag:subnet-router"},
		}
	}
	if _, err := coordinator.issue(context.Background(), testEnrollmentIdentity(fixtureWorkspaceID), request{}); err == nil {
		t.Fatal("full global Headscale key inventory admitted another key")
	}
	for _, command := range cli.commands {
		if len(command) > 1 && command[0] == "preauthkeys" && command[1] == "create" {
			t.Fatal("inventory limit still invoked Headscale key creation")
		}
	}
}

func TestCreatedWorkspacePreAuthKeyRequiresSingleUseEphemeralShape(t *testing.T) {
	now := time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)
	valid := headscalePreAuthKey{
		User: &headscalePreAuthKeyUser{ID: 17}, ID: 1, Key: "hskey-auth-secret", Ephemeral: true,
		Expiration: testProtobufTimestamp(now.Add(workspacePreAuthKeyLifetime)), CreatedAt: testProtobufTimestamp(now),
	}
	for name, mutate := range map[string]func(*headscalePreAuthKey){
		"missing identifier": func(key *headscalePreAuthKey) { key.ID = 0 },
		"reusable":           func(key *headscalePreAuthKey) { key.Reusable = true },
		"not ephemeral":      func(key *headscalePreAuthKey) { key.Ephemeral = false },
		"used":               func(key *headscalePreAuthKey) { key.Used = true },
		"wrong user":         func(key *headscalePreAuthKey) { key.User.ID = 18 },
		"tagged":             func(key *headscalePreAuthKey) { key.ACLTags = []string{"tag:unexpected"} },
		"long lifetime": func(key *headscalePreAuthKey) {
			key.Expiration = testProtobufTimestamp(now.Add(time.Hour))
		},
	} {
		t.Run(name, func(t *testing.T) {
			candidate := valid
			user := *valid.User
			candidate.User = &user
			mutate(&candidate)
			data, err := json.Marshal(candidate)
			if err != nil {
				t.Fatal(err)
			}
			if parsed, err := parseCreatedWorkspacePreAuthKey(data, 17, now); err == nil {
				t.Fatalf("unsafe Headscale create response was accepted: %#v", parsed)
			}
		})
	}
}

func testProtobufTimestamp(value time.Time) *protobufTimestamp {
	return &protobufTimestamp{Seconds: value.Unix(), Nanos: int32(value.Nanosecond())}
}
