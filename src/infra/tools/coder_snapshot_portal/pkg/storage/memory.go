// Implements an in-memory snapshot storage driver for local testing and development.

package storage

import (
	"context"
	"sort"
	"sync"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
)

// MemoryStore provides a concurrent-safe, in-memory implementation of SnapshotStore.
type MemoryStore struct {
	mu        sync.RWMutex
	snapshots map[string]map[string]*model.SnapshotManifest // userID -> selector -> manifest
}

// NewMemoryStore creates an empty in-memory snapshot store.
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		snapshots: make(map[string]map[string]*model.SnapshotManifest),
	}
}

// SaveSnapshot adds or updates a snapshot manifest in the in-memory store.
func (m *MemoryStore) SaveSnapshot(ctx context.Context, userID string, snapshot *model.SnapshotManifest) error {
	if err := validateUserID(userID); err != nil {
		return err
	}
	if snapshot == nil || snapshot.Selector == "" {
		return ErrInvalidSelector
	}
	if err := validateSelector(snapshot.Selector); err != nil {
		return err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	userStore, exists := m.snapshots[userID]
	if !exists {
		userStore = make(map[string]*model.SnapshotManifest)
		m.snapshots[userID] = userStore
	}

	cloned := *snapshot
	userStore[snapshot.Selector] = &cloned
	return nil
}

// DeleteSnapshot removes a snapshot manifest from the store.
func (m *MemoryStore) DeleteSnapshot(ctx context.Context, userID, selector string) error {
	if err := validateUserID(userID); err != nil {
		return err
	}
	if err := validateSelector(selector); err != nil {
		return err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	userStore, exists := m.snapshots[userID]
	if !exists {
		return ErrNotFound
	}

	if _, found := userStore[selector]; !found {
		return ErrNotFound
	}

	delete(userStore, selector)
	return nil
}

// ListUserSnapshots returns all snapshots stored for the given user ID.
func (m *MemoryStore) ListUserSnapshots(ctx context.Context, userID string) ([]*model.SnapshotManifest, error) {
	if err := validateUserID(userID); err != nil {
		return nil, err
	}

	m.mu.RLock()
	defer m.mu.RUnlock()

	userStore, exists := m.snapshots[userID]
	if !exists {
		return []*model.SnapshotManifest{}, nil
	}

	result := make([]*model.SnapshotManifest, 0, len(userStore))
	for _, snap := range userStore {
		cloned := *snap
		result = append(result, &cloned)
	}

	sort.Slice(result, func(i, j int) bool {
		if result[i].Timestamp != result[j].Timestamp {
			return result[i].Timestamp > result[j].Timestamp
		}
		return result[i].Selector < result[j].Selector
	})

	return result, nil
}

// GetSnapshot retrieves a snapshot by user ID and selector.
func (m *MemoryStore) GetSnapshot(ctx context.Context, userID, selector string) (*model.SnapshotManifest, error) {
	if err := validateUserID(userID); err != nil {
		return nil, err
	}
	if err := validateSelector(selector); err != nil {
		return nil, err
	}

	m.mu.RLock()
	defer m.mu.RUnlock()

	userStore, exists := m.snapshots[userID]
	if !exists {
		return nil, ErrNotFound
	}

	snap, found := userStore[selector]
	if !found {
		return nil, ErrNotFound
	}

	cloned := *snap
	return &cloned, nil
}
