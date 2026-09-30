// Defines the storage interface and error contracts for snapshot manifest retrieval.

package storage

import (
	"context"
	"errors"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
)

var (
	// ErrNotFound is returned when a requested snapshot manifest does not exist.
	ErrNotFound = errors.New("snapshot not found")
	// ErrInvalidUserID indicates the user identifier is malformed or violates scope boundaries.
	ErrInvalidUserID = errors.New("invalid user id")
	// ErrInvalidSelector indicates the snapshot selector contains invalid characters.
	ErrInvalidSelector = errors.New("invalid snapshot selector")
)

// SnapshotStore defines the contract for accessing owner-scoped snapshot manifests.
type SnapshotStore interface {
	// ListUserSnapshots retrieves all snapshot manifests belonging to the specified user.
	ListUserSnapshots(ctx context.Context, userID string) ([]*model.SnapshotManifest, error)

	// GetSnapshot retrieves a single snapshot manifest by its selector.
	GetSnapshot(ctx context.Context, userID string, selector string) (*model.SnapshotManifest, error)
}
