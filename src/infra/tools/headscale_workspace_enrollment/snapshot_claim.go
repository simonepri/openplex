// Signs and validates HMAC claims binding Kopia backup manifests to attested Coder workspace owners.

package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"regexp"
	"strings"
	"time"
)

const snapshotClaimKeySize = 32

const snapshotClockSkew = 5 * time.Minute

var (
	incarnationName  = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,62}$`)
	lineageName      = regexp.MustCompile(`^(?:[0-9a-f-]{36}-[0-9]{10,}|[a-z][a-z0-9-]{1,61}[a-z0-9]|[0-9a-f]{40})$`)
	principalID      = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
	snapshotSelector = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
)

type snapshotSignRequest struct {
	Selector string `json:"selector"`
	Time     string `json:"time"`
}

type snapshotClaim struct {
	Schema         int    `json:"schema"`
	Selector       string `json:"selector"`
	OwnerID        string `json:"ownerId"`
	PrincipalID    string `json:"principalId"`
	Team           string `json:"team"`
	Cell           string `json:"cell"`
	Incarnation    string `json:"incarnation"`
	Lineage        string `json:"lineage"`
	ParentLineage  string `json:"parentLineage,omitempty"`
	ParentSnapshot string `json:"parentSnapshot,omitempty"`
	IsRoot         bool   `json:"isRoot"`
	Workspace      string `json:"workspace"`
	SourceHost     string `json:"sourceHost"`
	SourceUser     string `json:"sourceUser"`
	SourcePath     string `json:"sourcePath"`
	StorageKey     string `json:"storageKey"`
	Time           string `json:"time"`
}

func (s server) signSnapshot(w http.ResponseWriter, r *http.Request) {
	id, err := s.authenticateAgent(r.Context(), r.Header.Get("Authorization"))
	if err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	var req snapshotSignRequest
	if decodeStrictJSON(r.Body, &req) != nil || !snapshotSelector.MatchString(req.Selector) {
		http.Error(w, "invalid snapshot", http.StatusBadRequest)
		return
	}
	snapshotTime, err := time.Parse(time.RFC3339Nano, req.Time)
	now := time.Now()
	if s.snapshotNow != nil {
		now = s.snapshotNow()
	}
	if err != nil || snapshotTime.After(now.Add(snapshotClockSkew)) {
		http.Error(w, "invalid snapshot", http.StatusBadRequest)
		return
	}
	claim := snapshotClaim{
		Schema: 3, Selector: req.Selector, OwnerID: id.SubjectID,
		PrincipalID: snapshotPrincipalID(id.OIDCIssuer, id.OIDCSubject),
		Team:        id.Team, Cell: id.Cell, Incarnation: id.Incarnation,
		Lineage: id.Lineage, ParentLineage: id.ParentLineage,
		ParentSnapshot: id.ParentSnapshot, IsRoot: id.IsRoot,
		Workspace: id.Workspace,
		SourceHost: id.Machine, SourceUser: id.IdPUsername,
		SourcePath: id.WorkspaceVolume, Time: req.Time,
	}
	claim.StorageKey = snapshotClaimStorageKey(snapshotTime, claim.Selector)
	if err := validateSnapshotClaim(claim); err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	if _, err := s.repositoryPasswordForIdentity(id); err != nil {
		http.Error(w, "snapshot repository unavailable", http.StatusServiceUnavailable)
		return
	}
	token, err := signSnapshotClaim(snapshotSigningKey(s.snapshotRootKey), claim)
	if err != nil {
		http.Error(w, "snapshot signing failed", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{
		"claim": token,
		"key":   claim.StorageKey,
	})
}

func (s server) snapshotRepository(w http.ResponseWriter, r *http.Request) {
	id, err := s.authenticateAgent(r.Context(), r.Header.Get("Authorization"))
	if err != nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	password, err := s.repositoryPasswordForIdentity(id)
	if err != nil {
		http.Error(w, "snapshot repository unavailable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"repositoryPassword": password})
}

func (s server) repositoryPasswordForIdentity(id identity) (string, error) {
	bound, err := s.bindingForOwner(id.SubjectID)
	if err != nil || bound.Issuer != id.OIDCIssuer || bound.Subject != id.OIDCSubject ||
		bound.PreferredUsername != id.IdPUsername {
		return "", errors.New("snapshot repository identity mismatch")
	}
	return snapshotRepositoryPassword(s.snapshotRootKey, snapshotPrincipalID(bound.Issuer, bound.Subject)), nil
}

func mustSnapshotRootKey(name string) []byte {
	key := []byte(mustEnv(name))
	if len(key) < snapshotClaimKeySize {
		panic(name + " must contain at least 32 bytes")
	}
	return key
}

func snapshotSigningKey(root []byte) []byte {
	mac := hmac.New(sha256.New, root)
	_, _ = mac.Write([]byte("workspace-snapshot-claim-signing"))
	return mac.Sum(nil)
}

func snapshotRepositoryPassword(root []byte, principal string) string {
	mac := hmac.New(sha256.New, root)
	_, _ = mac.Write([]byte("workspace-snapshot-repository-password\x00" + principal))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func snapshotClaimStorageKey(snapshotTime time.Time, selector string) string {
	return fmt.Sprintf("%019d-%s.jwt", math.MaxInt64-snapshotTime.UnixNano(), selector)
}

func (s server) authenticateAgent(ctx context.Context, authorization string) (identity, error) {
	token := strings.TrimPrefix(authorization, "Bearer ")
	if token == "" {
		return identity{}, errors.New("missing agent token")
	}
	id, err := s.authenticator.authenticate(ctx, token)
	if err != nil || !userID.MatchString(id.SubjectID) {
		return identity{}, errors.New("invalid agent identity")
	}
	bound, err := s.bindingForOwner(id.SubjectID)
	if err != nil || !loginName.MatchString(bound.PreferredUsername) {
		return identity{}, errors.New("identity is not bound")
	}
	id.OIDCIssuer = bound.Issuer
	id.OIDCSubject = bound.Subject
	id.IdPUsername = bound.PreferredUsername
	return id, nil
}

func signSnapshotClaim(key []byte, claim snapshotClaim) (string, error) {
	if err := validateSnapshotClaim(claim); err != nil {
		return "", err
	}
	payload, err := json.Marshal(claim)
	if err != nil {
		return "", err
	}
	encoded := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(encoded))
	signature := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	return encoded + "." + signature, nil
}

func verifySnapshotClaim(key []byte, token string) (snapshotClaim, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return snapshotClaim{}, errors.New("invalid signed snapshot claim")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return snapshotClaim{}, err
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(parts[0]))
	if !hmac.Equal(signature, mac.Sum(nil)) {
		return snapshotClaim{}, errors.New("invalid snapshot claim signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return snapshotClaim{}, err
	}
	var claim snapshotClaim
	if err := decodeStrictJSON(strings.NewReader(string(payload)), &claim); err != nil {
		return snapshotClaim{}, err
	}
	return claim, validateSnapshotClaim(claim)
}

func validSnapshotClaimFields(claim snapshotClaim) bool {
	validSchema := claim.Schema == 2 || claim.Schema == 3
	return validSchema &&
		snapshotSelector.MatchString(claim.Selector) &&
		userID.MatchString(claim.OwnerID) &&
		label.MatchString(claim.Team) &&
		principalID.MatchString(claim.PrincipalID) &&
		label.MatchString(claim.Cell) &&
		incarnationName.MatchString(claim.Incarnation) &&
		lineageName.MatchString(claim.Lineage) &&
		label.MatchString(claim.Workspace) &&
		label.MatchString(claim.SourceHost) &&
		loginName.MatchString(claim.SourceUser) &&
		claim.SourcePath == "/var/lib/workspace"
}

func validateSnapshotAncestry(claim snapshotClaim) error {
	if claim.Schema != 3 {
		return nil
	}
	if claim.IsRoot {
		if claim.ParentLineage != "" || claim.ParentSnapshot != "" {
			return errors.New("root snapshot claim must not specify parent ancestry")
		}
		return nil
	}
	if !lineageName.MatchString(claim.ParentLineage) || !snapshotSelector.MatchString(claim.ParentSnapshot) {
		return errors.New("non-root snapshot claim must specify valid parent lineage and snapshot")
	}
	return nil
}

func validateSnapshotTimeAndStorage(claim snapshotClaim) error {
	snapshotTime, err := time.Parse(time.RFC3339Nano, claim.Time)
	if err != nil || claim.StorageKey != snapshotClaimStorageKey(snapshotTime, claim.Selector) {
		return errors.New("invalid snapshot claim time")
	}
	return nil
}

func validateSnapshotClaim(claim snapshotClaim) error {
	if !validSnapshotClaimFields(claim) {
		return errors.New("invalid snapshot claim fields")
	}
	if err := validateSnapshotAncestry(claim); err != nil {
		return err
	}
	return validateSnapshotTimeAndStorage(claim)
}

func decodeStrictJSON(reader io.Reader, destination any) error {
	decoder := json.NewDecoder(io.LimitReader(reader, maxBody))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return errors.New("request contains trailing JSON")
	}
	return nil
}

