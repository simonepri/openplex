// Manages durable lineage claims to prevent concurrent Kopia backup writes across Coder builds.

package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

const (
	lineageClaimFileLimit = 4 << 20
	lineageClaimLimit     = 10000
	lineageClaimTTL       = 2 * time.Minute
)

type lineageRequest struct {
	Generation uint64 `json:"generation,omitempty"`
	Nonce      string `json:"nonce"`
}

type lineageClaimRecord struct {
	AgentID     string `json:"agentId"`
	Cell        string `json:"cell"`
	ExpiresAt   string `json:"expiresAt"`
	Generation  uint64 `json:"generation"`
	Lineage     string `json:"lineage"`
	NonceHash   string `json:"nonceHash"`
	OwnerID     string `json:"ownerId"`
	PrincipalID string `json:"principalId"`
	Team        string `json:"team"`
	Workspace   string `json:"workspace"`
	WorkspaceID string `json:"workspaceId"`
}

type lineageClaimFile struct {
	Claims         []lineageClaimRecord `json:"claims"`
	LastGeneration uint64               `json:"lastGeneration"`
	Schema         int                  `json:"schema"`
}

func (s server) acquireLineage(w http.ResponseWriter, r *http.Request) {
	s.mutateLineage(w, r, "acquire")
}

func (s server) renewLineage(w http.ResponseWriter, r *http.Request) {
	s.mutateLineage(w, r, "renew")
}

func (s server) releaseLineage(w http.ResponseWriter, r *http.Request) {
	s.mutateLineage(w, r, "release")
}

func validateLineageOperationRequest(operation string, req lineageRequest) ([]byte, error) {
	nonce, err := lineageNonce(req.Nonce)
	if err != nil {
		return nil, err
	}
	if operation == "acquire" && req.Generation != 0 {
		return nil, errors.New("acquire requires generation 0")
	}
	if operation != "acquire" && (req.Generation == 0 || req.Generation > math.MaxInt64) {
		return nil, errors.New("non-acquire requires non-zero generation within bounds")
	}
	return nonce, nil
}

func (s server) currentTimes() (time.Time, time.Time) {
	wallNow := time.Now().UTC()
	if s.lineageWallNow != nil {
		wallNow = s.lineageWallNow().UTC()
	}
	monotonicNow := time.Now()
	if s.lineageMonotonicNow != nil {
		monotonicNow = s.lineageMonotonicNow()
	}
	return wallNow, monotonicNow
}

func applyLineageOperation(
	operation string,
	state lineageClaimFile,
	index int,
	id identity,
	nonce []byte,
	generation uint64,
	wallNow time.Time,
) (lineageClaimFile, uint64, error) {
	var err error
	switch operation {
	case "acquire":
		return acquireLineageRecord(state, index, id, nonce, wallNow)
	case "renew":
		state.Claims, err = renewLineageRecord(state.Claims, index, id, nonce, generation, wallNow)
		return state, generation, err
	case "release":
		state.Claims, err = releaseLineageRecord(state.Claims, index, id, nonce, generation)
		return state, generation, err
	default:
		return state, 0, errors.New("invalid lineage operation")
	}
}

func (s server) mutateLineage(w http.ResponseWriter, r *http.Request, operation string) {
	id, err := s.authenticateAgent(r.Context(), r.Header.Get("Authorization"))
	if err != nil || validateLineageIdentity(id) != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	var req lineageRequest
	if decodeStrictJSON(r.Body, &req) != nil {
		http.Error(w, "invalid lineage claim", http.StatusBadRequest)
		return
	}
	nonce, err := validateLineageOperationRequest(operation, req)
	if err != nil {
		http.Error(w, "invalid lineage claim", http.StatusBadRequest)
		return
	}
	wallNow, monotonicNow := s.currentTimes()

	s.lineagesMu.Lock()
	defer s.lineagesMu.Unlock()
	state, err := readLineageClaims(s.lineagePath)
	if err != nil {
		http.Error(w, "lineage claim unavailable", http.StatusServiceUnavailable)
		return
	}
	state.Claims = pruneInactiveLineageRecords(
		state.Claims,
		s.lineageDeadlines,
		s.lineageStarted,
		monotonicNow,
	)
	index := lineageRecordIndex(state.Claims, id)
	state, generation, err := applyLineageOperation(operation, state, index, id, nonce, req.Generation, wallNow)
	if err != nil {
		http.Error(w, "lineage is held by another build", http.StatusConflict)
		return
	}
	if err := writeLineageClaims(s.lineagePath, state); err != nil {
		http.Error(w, "lineage claim unavailable", http.StatusServiceUnavailable)
		return
	}
	claimKey := lineageIdentityKey(id)
	if operation == "release" {
		delete(s.lineageDeadlines, claimKey)
	} else {
		s.lineageDeadlines[claimKey] = monotonicNow.Add(lineageClaimTTL)
	}
	if operation != "acquire" {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = fmt.Fprint(w, generation)
}

func acquireLineageRecord(
	state lineageClaimFile,
	index int,
	id identity,
	nonce []byte,
	now time.Time,
) (lineageClaimFile, uint64, error) {
	if index >= 0 {
		current := state.Claims[index]
		if !lineageHolderMatches(current, id, nonce) {
			return lineageClaimFile{}, 0, errors.New("lineage is already held")
		}
		current.ExpiresAt = now.Add(lineageClaimTTL).Format(time.RFC3339Nano)
		state.Claims[index] = current
		return state, current.Generation, nil
	}
	if len(state.Claims) >= lineageClaimLimit || state.LastGeneration >= math.MaxInt64 {
		return lineageClaimFile{}, 0, errors.New("lineage claim limit exceeded")
	}
	state.LastGeneration++
	state.Claims = append(state.Claims, newLineageRecord(id, nonce, state.LastGeneration, now))
	return state, state.LastGeneration, nil
}

func renewLineageRecord(
	claims []lineageClaimRecord,
	index int,
	id identity,
	nonce []byte,
	generation uint64,
	now time.Time,
) ([]lineageClaimRecord, error) {
	if index < 0 || claims[index].Generation != generation || !lineageHolderMatches(claims[index], id, nonce) {
		return nil, errors.New("lineage claim is not held")
	}
	claims[index].ExpiresAt = now.Add(lineageClaimTTL).Format(time.RFC3339Nano)
	return claims, nil
}

func releaseLineageRecord(
	claims []lineageClaimRecord,
	index int,
	id identity,
	nonce []byte,
	generation uint64,
) ([]lineageClaimRecord, error) {
	if index < 0 {
		return claims, nil
	}
	current := claims[index]
	if current.Generation != generation || !lineageHolderMatches(current, id, nonce) {
		return nil, errors.New("lineage claim is held by another build")
	}
	return append(claims[:index], claims[index+1:]...), nil
}

func pruneInactiveLineageRecords(
	claims []lineageClaimRecord,
	deadlines map[string]time.Time,
	started time.Time,
	now time.Time,
) []lineageClaimRecord {
	active := claims[:0]
	for _, claim := range claims {
		key := lineageRecordKey(claim)
		deadline, known := deadlines[key]
		if (known && deadline.After(now)) || (!known && now.Before(started.Add(lineageClaimTTL))) {
			active = append(active, claim)
			continue
		}
		delete(deadlines, key)
	}
	return active
}

func newLineageRecord(id identity, nonce []byte, generation uint64, now time.Time) lineageClaimRecord {
	nonceHash := sha256.Sum256(nonce)
	return lineageClaimRecord{
		AgentID: id.AgentID, Cell: id.Cell,
		ExpiresAt:  now.Add(lineageClaimTTL).Format(time.RFC3339Nano),
		Generation: generation, Lineage: id.Lineage,
		NonceHash:   base64.RawURLEncoding.EncodeToString(nonceHash[:]),
		OwnerID:     id.SubjectID,
		PrincipalID: snapshotPrincipalID(id.OIDCIssuer, id.OIDCSubject),
		Team:        id.Team,
		Workspace:   id.Workspace, WorkspaceID: id.WorkspaceID,
	}
}

func lineageRecordIndex(claims []lineageClaimRecord, id identity) int {
	principal := snapshotPrincipalID(id.OIDCIssuer, id.OIDCSubject)
	match := -1
	for index, claim := range claims {
		if claim.PrincipalID != principal || claim.Team != id.Team || claim.Lineage != id.Lineage {
			continue
		}
		if match >= 0 {
			return -2
		}
		match = index
	}
	return match
}

func lineageIdentityKey(id identity) string {
	return snapshotPrincipalID(id.OIDCIssuer, id.OIDCSubject) + "\x00" + id.Team + "\x00" + id.Lineage
}

func lineageRecordKey(claim lineageClaimRecord) string {
	return claim.PrincipalID + "\x00" + claim.Team + "\x00" + claim.Lineage
}

func lineageHolderMatches(claim lineageClaimRecord, id identity, nonce []byte) bool {
	wantHash := sha256.Sum256(nonce)
	storedHash, err := base64.RawURLEncoding.DecodeString(claim.NonceHash)
	return err == nil &&
		hmac.Equal(storedHash, wantHash[:]) &&
		claim.AgentID == id.AgentID &&
		claim.Cell == id.Cell && claim.OwnerID == id.SubjectID &&
		claim.PrincipalID == snapshotPrincipalID(id.OIDCIssuer, id.OIDCSubject) &&
		claim.Team == id.Team &&
		claim.Workspace == id.Workspace && claim.WorkspaceID == id.WorkspaceID
}

func validateLineageIdentity(id identity) error {
	if !userID.MatchString(id.AgentID) ||
		!label.MatchString(id.Cell) || !lineageName.MatchString(id.Lineage) || !label.MatchString(id.Team) ||
		!userID.MatchString(id.SubjectID) || !userID.MatchString(id.WorkspaceID) ||
		!label.MatchString(id.Workspace) || id.OIDCIssuer == "" || id.OIDCSubject == "" {
		return errors.New("invalid lineage identity")
	}
	return nil
}

func lineageNonce(encoded string) ([]byte, error) {
	nonce, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil || len(nonce) != 32 || base64.RawURLEncoding.EncodeToString(nonce) != encoded {
		return nil, errors.New("invalid lineage nonce")
	}
	return nonce, nil
}

func readLineageClaims(path string) (lineageClaimFile, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return lineageClaimFile{Schema: 1}, nil
	}
	if err != nil {
		return lineageClaimFile{}, err
	}
	defer file.Close()
	decoder := json.NewDecoder(io.LimitReader(file, lineageClaimFileLimit+1))
	decoder.DisallowUnknownFields()
	var state lineageClaimFile
	if err := decoder.Decode(&state); err != nil || decoder.Decode(&struct{}{}) != io.EOF ||
		state.Schema != 1 || len(state.Claims) > lineageClaimLimit {
		return lineageClaimFile{}, errors.New("invalid lineage claim state")
	}
	seen := map[string]struct{}{}
	for _, claim := range state.Claims {
		if err := validateLineageRecord(claim); err != nil || claim.Generation > state.LastGeneration {
			return lineageClaimFile{}, errors.New("invalid lineage claim state")
		}
		key := claim.PrincipalID + "\x00" + claim.Team + "\x00" + claim.Lineage
		if _, exists := seen[key]; exists {
			return lineageClaimFile{}, errors.New("duplicate lineage claim")
		}
		seen[key] = struct{}{}
	}
	return state, nil
}

func validateLineageRecord(claim lineageClaimRecord) error {
	if !userID.MatchString(claim.AgentID) ||
		!label.MatchString(claim.Cell) || claim.Generation == 0 ||
		!lineageName.MatchString(claim.Lineage) || !principalID.MatchString(claim.PrincipalID) ||
		!label.MatchString(claim.Team) ||
		!userID.MatchString(claim.OwnerID) || !userID.MatchString(claim.WorkspaceID) ||
		!label.MatchString(claim.Workspace) {
		return errors.New("invalid lineage claim state")
	}
	if _, err := time.Parse(time.RFC3339Nano, claim.ExpiresAt); err != nil {
		return errors.New("invalid lineage claim expiry")
	}
	decoded, err := base64.RawURLEncoding.DecodeString(claim.NonceHash)
	if err != nil || len(decoded) != sha256.Size || base64.RawURLEncoding.EncodeToString(decoded) != claim.NonceHash {
		return errors.New("invalid lineage claim nonce hash")
	}
	return nil
}

func writeLineageClaims(path string, state lineageClaimFile) error {
	if len(state.Claims) > lineageClaimLimit || state.LastGeneration > math.MaxInt64 {
		return errors.New("lineage claim limit exceeded")
	}
	state.Schema = 1
	data, err := json.Marshal(state)
	if err != nil || len(data) > lineageClaimFileLimit {
		return errors.New("lineage claim state exceeds its limit")
	}
	directory := filepath.Dir(path)
	temporary, err := os.OpenFile(path+".tmp", os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	if _, err = temporary.Write(append(data, '\n')); err == nil {
		err = temporary.Sync()
	}
	closeErr := temporary.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		return err
	}
	directoryHandle, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer directoryHandle.Close()
	if err := directoryHandle.Sync(); err != nil {
		return fmt.Errorf("sync lineage claim directory: %w", err)
	}
	return nil
}
