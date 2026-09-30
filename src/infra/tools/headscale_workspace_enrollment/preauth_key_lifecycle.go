// Manages bounded durable Headscale preauth keys and coordinates CLI lifecycle operations.

package main

import (
	"bytes"
	"context"
	"errors"
	"log"
	"os"
	"sort"
	"strconv"
	"time"
)

const (
	headscalePreAuthKeyInventoryLimit  = 128
	workspacePreAuthKeyFileLimit       = 128 << 10
	workspacePreAuthKeyLimit           = 64
	workspacePreAuthKeyReconcilePeriod = 30 * time.Second
	workspacePreAuthKeySchema          = 1
	workspacePreAuthKeyLifetime        = 10 * time.Minute
	workspacePreAuthKeyLifetimeFlag    = "10m"
)

type protobufTimestamp struct {
	Seconds int64 `json:"seconds,omitempty"`
	Nanos   int32 `json:"nanos,omitempty"`
}

type headscalePreAuthKeyUser struct {
	ID uint64 `json:"id,omitempty"`
}

type headscalePreAuthKey struct {
	User       *headscalePreAuthKeyUser `json:"user,omitempty"`
	ID         uint64                   `json:"id,omitempty"`
	Key        string                   `json:"key,omitempty"`
	Reusable   bool                     `json:"reusable,omitempty"`
	Ephemeral  bool                     `json:"ephemeral,omitempty"`
	Used       bool                     `json:"used,omitempty"`
	Expiration *protobufTimestamp       `json:"expiration,omitempty"`
	CreatedAt  *protobufTimestamp       `json:"created_at,omitempty"`
	ACLTags    []string                 `json:"acl_tags,omitempty"`
}

type workspacePreAuthKey struct {
	BaselineIDs []uint64  `json:"baselineIds,omitempty"`
	ExpiresAt   time.Time `json:"expiresAt,omitempty"`
	KeyID       uint64    `json:"keyId,omitempty"`
	OwnerID     string    `json:"ownerId"`
	StartedAt   time.Time `json:"startedAt"`
	UserID      uint64    `json:"userId"`
	WorkspaceID string    `json:"workspaceId"`
}

type workspacePreAuthKeyFile struct {
	Keys   []workspacePreAuthKey `json:"keys"`
	Schema int                   `json:"schema"`
}

func initializeWorkspacePreAuthKeys(path string) error {
	_, err := os.Stat(path)
	if err == nil {
		_, err = readWorkspacePreAuthKeys(path)
		return err
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return writeWorkspacePreAuthKeys(path, nil)
}

func (c *commandCoordinator) resolveIssuanceIdentity(ctx context.Context, id identity) (string, uint64, error) {
	if c.keyMu == nil || c.keyPath == "" {
		return "", 0, errors.New("workspace preauth key lifecycle is unavailable")
	}
	userText, err := c.userID(ctx, id)
	if err != nil {
		return "", 0, err
	}
	userID, err := strconv.ParseUint(userText, 10, 64)
	if err != nil || userID == 0 || !userIDPattern(id.SubjectID, id.WorkspaceID) {
		return "", 0, errors.New("invalid workspace preauth key identity")
	}
	return userText, userID, nil
}

func (c *commandCoordinator) pruneMatchingKeys(ctx context.Context, keys []workspacePreAuthKey, inventory map[uint64]headscalePreAuthKey, binding string) ([]workspacePreAuthKey, error) {
	retained := make([]workspacePreAuthKey, 0, len(keys))
	for _, current := range keys {
		if workspacePreAuthKeyBinding(current.OwnerID, current.WorkspaceID) != binding {
			retained = append(retained, current)
			continue
		}
		if err := c.deletePreAuthKey(ctx, current.KeyID); err != nil {
			return nil, err
		}
		delete(inventory, current.KeyID)
	}
	return retained, nil
}

func (c *commandCoordinator) finalizeIssuedKey(ctx context.Context, keys []workspacePreAuthKey, created headscalePreAuthKey, binding string) (string, error) {
	for index := range keys {
		if workspacePreAuthKeyBinding(keys[index].OwnerID, keys[index].WorkspaceID) != binding {
			continue
		}
		keys[index].BaselineIDs = nil
		keys[index].ExpiresAt = created.expirationTime()
		keys[index].KeyID = created.ID
		if err := writeWorkspacePreAuthKeys(c.keyPath, keys); err != nil {
			deleteErr := c.deletePreAuthKey(ctx, created.ID)
			cleanupErr := c.reconcileAfterFailedIssuance(ctx)
			return "", errors.Join(err, deleteErr, cleanupErr)
		}
		return created.Key, nil
	}
	return "", errors.New("workspace preauth key intent disappeared")
}

func (c *commandCoordinator) issue(ctx context.Context, id identity, _ request) (string, error) {
	userText, userID, err := c.resolveIssuanceIdentity(ctx, id)
	if err != nil {
		return "", err
	}

	c.keyMu.Lock()
	defer c.keyMu.Unlock()
	inventory, keys, err := c.reconcilePreAuthKeysLocked(ctx)
	if err != nil {
		return "", err
	}
	binding := workspacePreAuthKeyBinding(id.SubjectID, id.WorkspaceID)
	keys, err = c.pruneMatchingKeys(ctx, keys, inventory, binding)
	if err != nil {
		return "", err
	}
	if len(keys) >= workspacePreAuthKeyLimit {
		return "", errors.New("workspace preauth key issuance limit reached")
	}
	if len(inventory) >= headscalePreAuthKeyInventoryLimit {
		return "", errors.New("Headscale preauth key inventory limit reached")
	}

	now := c.wallNow()
	pending := workspacePreAuthKey{
		BaselineIDs: sortedPreAuthKeyIDs(inventory),
		OwnerID:     id.SubjectID,
		StartedAt:   now,
		UserID:      userID,
		WorkspaceID: id.WorkspaceID,
	}
	keys = append(keys, pending)
	if err := writeWorkspacePreAuthKeys(c.keyPath, keys); err != nil {
		return "", err
	}

	out, err := c.execute(ctx, "preauthkeys", "create", "--user", userText,
		"--expiration", workspacePreAuthKeyLifetimeFlag, "--ephemeral", "--output", "json")
	if err != nil {
		return "", errors.Join(err, c.reconcileAfterFailedIssuance(ctx))
	}
	created, err := parseCreatedWorkspacePreAuthKey(out, userID, now)
	if err != nil {
		return "", errors.Join(err, c.reconcileAfterFailedIssuance(ctx))
	}
	return c.finalizeIssuedKey(ctx, keys, created, binding)
}

func (c *commandCoordinator) reconcileAfterFailedIssuance(ctx context.Context) error {
	_, _, err := c.reconcilePreAuthKeysLocked(ctx)
	return err
}

func (c *commandCoordinator) reconcilePreAuthKeysLoop(ctx context.Context) {
	reconcile := func() {
		if err := c.reconcilePreAuthKeys(ctx); err != nil {
			log.Printf("reconcile workspace preauth keys: %v", err)
		}
	}
	reconcile()
	ticker := time.NewTicker(workspacePreAuthKeyReconcilePeriod)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			reconcile()
		}
	}
}

func (c *commandCoordinator) reconcilePreAuthKeys(ctx context.Context) error {
	if c.keyMu == nil || c.keyPath == "" {
		return errors.New("workspace preauth key lifecycle is unavailable")
	}
	c.keyMu.Lock()
	defer c.keyMu.Unlock()
	_, _, err := c.reconcilePreAuthKeysLocked(ctx)
	return err
}

func (c *commandCoordinator) reconcilePreAuthKeysLocked(
	ctx context.Context,
) (map[uint64]headscalePreAuthKey, []workspacePreAuthKey, error) {
	inventory, err := c.preAuthKeys(ctx)
	if err != nil {
		return nil, nil, err
	}
	keys, err := readWorkspacePreAuthKeys(c.keyPath)
	if err != nil {
		return nil, nil, err
	}
	now := c.wallNow()
	retained := make([]workspacePreAuthKey, 0, len(keys))
	for _, tracked := range keys {
		if tracked.KeyID == 0 {
			baseline := uint64Set(tracked.BaselineIDs)
			for keyID, candidate := range inventory {
				if _, existed := baseline[keyID]; existed || !pendingWorkspaceKey(candidate, tracked) {
					continue
				}
				if err := c.deletePreAuthKey(ctx, keyID); err != nil {
					return nil, nil, err
				}
				delete(inventory, keyID)
			}
			continue
		}
		current, exists := inventory[tracked.KeyID]
		if !exists {
			continue
		}
		if !activeWorkspaceKey(current, tracked) {
			continue
		}
		if current.Used || !tracked.ExpiresAt.After(now) {
			if err := c.deletePreAuthKey(ctx, tracked.KeyID); err != nil {
				return nil, nil, err
			}
			delete(inventory, tracked.KeyID)
			continue
		}
		retained = append(retained, tracked)
	}
	if err := writeWorkspacePreAuthKeys(c.keyPath, retained); err != nil {
		return nil, nil, err
	}
	return inventory, retained, nil
}

func (c *commandCoordinator) preAuthKeys(ctx context.Context) (map[uint64]headscalePreAuthKey, error) {
	out, err := c.execute(ctx, "preauthkeys", "list", "--output", "json")
	if err != nil {
		return nil, err
	}
	var listed []headscalePreAuthKey
	if decodeBoundedJSONBytes(out, &listed) != nil || len(listed) > headscalePreAuthKeyInventoryLimit {
		return nil, errors.New("Headscale returned an invalid preauth key inventory")
	}
	inventory := make(map[uint64]headscalePreAuthKey, len(listed))
	for _, key := range listed {
		if key.ID == 0 || len(key.Key) > 512 || len(key.ACLTags) > 32 ||
			(key.User != nil && key.User.ID == 0) || invalidTimestamp(key.Expiration) || invalidTimestamp(key.CreatedAt) {
			return nil, errors.New("Headscale returned an invalid preauth key")
		}
		for _, tag := range key.ACLTags {
			if len(tag) > 255 {
				return nil, errors.New("Headscale returned an invalid preauth key tag")
			}
		}
		if _, duplicate := inventory[key.ID]; duplicate {
			return nil, errors.New("Headscale returned a duplicate preauth key ID")
		}
		inventory[key.ID] = key
	}
	return inventory, nil
}

func (c *commandCoordinator) deletePreAuthKey(ctx context.Context, keyID uint64) error {
	if keyID == 0 {
		return errors.New("invalid Headscale preauth key ID")
	}
	_, err := c.execute(ctx, "preauthkeys", "delete", "--id", strconv.FormatUint(keyID, 10))
	return err
}

func validCreatedWorkspaceKey(k headscalePreAuthKey, userID uint64) bool {
	if !workspaceKeyShape(k, userID) {
		return false
	}
	if k.ID == 0 || k.Key == "" || len(k.Key) > 512 || k.Used {
		return false
	}
	return !invalidTimestamp(k.Expiration) && !invalidTimestamp(k.CreatedAt)
}

func validKeyLifetime(k headscalePreAuthKey, now time.Time) bool {
	createdAt := k.createdTime()
	expiresAt := k.expirationTime()
	if createdAt.Before(now.Add(-coordinatorCommandTimeout)) || createdAt.After(now.Add(coordinatorCommandTimeout)) {
		return false
	}
	return !expiresAt.Before(now.Add(workspacePreAuthKeyLifetime-coordinatorCommandTimeout)) &&
		!expiresAt.After(now.Add(workspacePreAuthKeyLifetime+coordinatorCommandTimeout))
}

func parseCreatedWorkspacePreAuthKey(data []byte, userID uint64, now time.Time) (headscalePreAuthKey, error) {
	var created headscalePreAuthKey
	if decodeBoundedJSONBytes(data, &created) != nil || !validCreatedWorkspaceKey(created, userID) {
		return headscalePreAuthKey{}, errors.New("Headscale returned an invalid workspace key")
	}
	if !validKeyLifetime(created, now) {
		return headscalePreAuthKey{}, errors.New("Headscale returned a workspace key outside its lifetime")
	}
	return created, nil
}

func activeWorkspaceKey(key headscalePreAuthKey, tracked workspacePreAuthKey) bool {
	return workspaceKeyShape(key, tracked.UserID) && key.ID == tracked.KeyID &&
		key.expirationTime().Equal(tracked.ExpiresAt)
}

func pendingWorkspaceKey(key headscalePreAuthKey, tracked workspacePreAuthKey) bool {
	if !workspaceKeyShape(key, tracked.UserID) {
		return false
	}
	createdAt := key.createdTime()
	return !createdAt.Before(tracked.StartedAt.Add(-coordinatorCommandTimeout)) &&
		key.expirationTime().Sub(createdAt) >= workspacePreAuthKeyLifetime-coordinatorCommandTimeout &&
		key.expirationTime().Sub(createdAt) <= workspacePreAuthKeyLifetime+coordinatorCommandTimeout
}

func workspaceKeyShape(key headscalePreAuthKey, userID uint64) bool {
	return key.User != nil && key.User.ID == userID && !key.Reusable && key.Ephemeral &&
		len(key.ACLTags) == 0 && key.Expiration != nil && key.CreatedAt != nil
}

func (key headscalePreAuthKey) expirationTime() time.Time {
	return time.Unix(key.Expiration.Seconds, int64(key.Expiration.Nanos)).UTC()
}

func (key headscalePreAuthKey) createdTime() time.Time {
	return time.Unix(key.CreatedAt.Seconds, int64(key.CreatedAt.Nanos)).UTC()
}

func invalidTimestamp(timestamp *protobufTimestamp) bool {
	return timestamp != nil && (timestamp.Seconds <= 0 || timestamp.Nanos < 0 || timestamp.Nanos >= int32(time.Second))
}

func (c *commandCoordinator) wallNow() time.Time {
	if c.now != nil {
		return c.now().UTC()
	}
	return time.Now().UTC()
}

func readWorkspacePreAuthKeys(path string) ([]workspacePreAuthKey, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	var state workspacePreAuthKeyFile
	if decodeBoundedStrictJSON(file, workspacePreAuthKeyFileLimit, &state) != nil ||
		state.Schema != workspacePreAuthKeySchema || validateWorkspacePreAuthKeys(state.Keys) != nil {
		return nil, errors.New("invalid workspace preauth key state")
	}
	return state.Keys, nil
}

func writeWorkspacePreAuthKeys(path string, keys []workspacePreAuthKey) error {
	if validateWorkspacePreAuthKeys(keys) != nil {
		return errors.New("invalid workspace preauth key state")
	}
	sort.Slice(keys, func(i, j int) bool {
		return workspacePreAuthKeyBinding(keys[i].OwnerID, keys[i].WorkspaceID) <
			workspacePreAuthKeyBinding(keys[j].OwnerID, keys[j].WorkspaceID)
	})
	return writeDurableJSON(path, workspacePreAuthKeyFile{Keys: keys, Schema: workspacePreAuthKeySchema}, workspacePreAuthKeyFileLimit)
}

func validateWorkspacePreAuthKeys(keys []workspacePreAuthKey) error {
	if len(keys) > workspacePreAuthKeyLimit {
		return errors.New("workspace preauth key limit exceeded")
	}
	bindings := make(map[string]struct{}, len(keys))
	keyIDs := make(map[uint64]struct{}, len(keys))
	pending := 0
	for _, key := range keys {
		binding := workspacePreAuthKeyBinding(key.OwnerID, key.WorkspaceID)
		if !userIDPattern(key.OwnerID, key.WorkspaceID) || key.UserID == 0 || key.StartedAt.IsZero() {
			return errors.New("invalid workspace preauth key")
		}
		if _, duplicate := bindings[binding]; duplicate {
			return errors.New("duplicate workspace preauth key binding")
		}
		bindings[binding] = struct{}{}
		if key.KeyID == 0 {
			pending++
			if pending > 1 || !key.ExpiresAt.IsZero() || len(key.BaselineIDs) > headscalePreAuthKeyInventoryLimit ||
				!strictlyIncreasingIDs(key.BaselineIDs) {
				return errors.New("invalid pending workspace preauth key")
			}
			continue
		}
		if !key.ExpiresAt.After(key.StartedAt) || len(key.BaselineIDs) != 0 {
			return errors.New("invalid active workspace preauth key")
		}
		if _, duplicate := keyIDs[key.KeyID]; duplicate {
			return errors.New("duplicate workspace preauth key ID")
		}
		keyIDs[key.KeyID] = struct{}{}
	}
	return nil
}

func userIDPattern(ownerID, workspaceID string) bool {
	return userID.MatchString(ownerID) && userID.MatchString(workspaceID)
}

func workspacePreAuthKeyBinding(ownerID, workspaceID string) string {
	return ownerID + "\x00" + workspaceID
}

func sortedPreAuthKeyIDs(inventory map[uint64]headscalePreAuthKey) []uint64 {
	ids := make([]uint64, 0, len(inventory))
	for id := range inventory {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}

func strictlyIncreasingIDs(ids []uint64) bool {
	for index, id := range ids {
		if id == 0 || index > 0 && ids[index-1] >= id {
			return false
		}
	}
	return true
}

func uint64Set(ids []uint64) map[uint64]struct{} {
	result := make(map[uint64]struct{}, len(ids))
	for _, id := range ids {
		result[id] = struct{}{}
	}
	return result
}

func decodeBoundedJSONBytes(data []byte, destination any) error {
	return decodeBoundedJSON(bytes.NewReader(data), maxCoordinatorOutput, destination)
}
