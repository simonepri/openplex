// Tests snapshot claim creation, metadata validation, signature verification, and tamper rejection.

package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestSnapshotClaimUsesAttestedAgentIdentity(t *testing.T) {
	s := snapshotServer(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/snapshots/sign", strings.NewReader(
		`{"selector":"manifest-1","time":"2026-09-03T10:15:30Z"}`,
	))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.signSnapshot(res, req)
	if res.Code != http.StatusOK {
		t.Fatalf("snapshot sign returned %d: %s", res.Code, res.Body.String())
	}
	var signed map[string]string
	if err := json.Unmarshal(res.Body.Bytes(), &signed); err != nil {
		t.Fatal(err)
	}
	claim, err := verifySnapshotClaim(snapshotSigningKey(s.snapshotRootKey), signed["claim"])
	if err != nil {
		t.Fatal(err)
	}
	if claim.OwnerID != fixtureUserID || claim.SourceUser != "ldap" ||
		claim.Schema != 3 || !claim.IsRoot ||
		claim.PrincipalID != snapshotPrincipalID("https://issuer.example", "dex-subject") ||
		claim.Workspace != "dev" || claim.SourceHost != "ldap-dev" ||
		claim.SourcePath != "/var/lib/workspace" || claim.Team != "examples" {
		t.Fatalf("claim was not bound to the attested agent: %#v", claim)
	}
	if signed["key"] != claim.StorageKey || len(signed) != 2 {
		t.Fatalf("snapshot response did not bind its storage key: %#v", signed)
	}
}

func TestSnapshotClaimRejectsTampering(t *testing.T) {
	s := snapshotServer(t)
	token, err := signSnapshotClaim(snapshotSigningKey(s.snapshotRootKey), fixtureSnapshotClaim())
	if err != nil {
		t.Fatal(err)
	}
	tampered := "A" + token[1:]
	if _, err := verifySnapshotClaim(snapshotSigningKey(s.snapshotRootKey), tampered); err == nil {
		t.Fatal("tampered snapshot claim was accepted")
	}
}

func TestSnapshotClaimRejectsPreviousRootEpoch(t *testing.T) {
	previousRoot := []byte("previous-snapshot-root-epoch-key")
	currentRoot := []byte("current-snapshot-root-epoch-key-")
	claim := fixtureSnapshotClaim()
	token, err := signSnapshotClaim(snapshotSigningKey(previousRoot), claim)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifySnapshotClaim(snapshotSigningKey(currentRoot), token); err == nil {
		t.Fatal("claim signed by the previous snapshot root was accepted")
	}
	principal := snapshotPrincipalID("https://issuer.example", "subject")
	if snapshotRepositoryPassword(previousRoot, principal) == snapshotRepositoryPassword(currentRoot, principal) {
		t.Fatal("previous snapshot root retained the current repository password")
	}
}

func TestSnapshotClaimRejectsUnattestedSourceMetadata(t *testing.T) {
	s := snapshotServer(t)
	s.authenticator = fixedAuthenticator{identity: identity{
		SubjectID: fixtureUserID, Team: "examples", Cell: "cell-eaws-lh1",
		Incarnation: "eaws-lh1", Lineage: "ldap-dev", Machine: "ldap-dev", Workspace: "dev",
		WorkspaceVolume: "/foreign/path",
	}}
	req := httptest.NewRequest(http.MethodPost, "/v1/snapshots/sign", strings.NewReader(
		`{"selector":"manifest-1","time":"2026-09-03T10:15:30Z"}`,
	))
	req.Header.Set("Authorization", "Bearer agent-token")
	res := httptest.NewRecorder()
	s.signSnapshot(res, req)
	if res.Code != http.StatusUnauthorized {
		t.Fatalf("unsafe source path returned %d", res.Code)
	}
}

func TestSnapshotRepositoryPasswordIsStableForAttestedOwner(t *testing.T) {
	s := snapshotServer(t)
	request := func() string {
		req := httptest.NewRequest(http.MethodPost, "/v1/snapshots/repository", nil)
		req.Header.Set("Authorization", "Bearer agent-token")
		res := httptest.NewRecorder()
		s.snapshotRepository(res, req)
		if res.Code != http.StatusOK {
			t.Fatalf("snapshot repository returned %d: %s", res.Code, res.Body.String())
		}
		var response map[string]string
		if err := json.Unmarshal(res.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if len(response) != 1 {
			t.Fatalf("repository response exposed undeclared fields: %#v", response)
		}
		return response["repositoryPassword"]
	}
	first := request()
	second := request()
	if len(first) != 43 || second != first {
		t.Fatalf("repository password was not a stable 256-bit value: %q %q", first, second)
	}
}

func TestSnapshotClaimAcceptsDigitLeadingRestoredLineage(t *testing.T) {
	claim := fixtureSnapshotClaim()
	claim.Lineage = "0123456789abcdef0123456789abcdef01234567"
	if _, err := signSnapshotClaim(make([]byte, snapshotClaimKeySize), claim); err != nil {
		t.Fatalf("digit-leading restored lineage was rejected: %v", err)
	}
	claim.Lineage = "0123456789abcdef0123456789abcdef0123456g"
	if _, err := signSnapshotClaim(make([]byte, snapshotClaimKeySize), claim); err == nil {
		t.Fatal("malformed restored lineage was accepted")
	}
}

func TestSnapshotClaimRejectsTimeOutsideSigningSkew(t *testing.T) {
	now := time.Date(2026, 9, 3, 10, 15, 30, 0, time.UTC)
	for _, test := range []struct {
		name   string
		offset time.Duration
		want   int
	}{
		{name: "long snapshot", offset: -30 * time.Minute, want: http.StatusOK},
		{name: "future boundary", offset: snapshotClockSkew, want: http.StatusOK},
		{name: "too far in future", offset: snapshotClockSkew + time.Nanosecond, want: http.StatusBadRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			s := snapshotServer(t)
			s.snapshotNow = func() time.Time { return now }
			body := `{"selector":"manifest-1","time":"` + now.Add(test.offset).Format(time.RFC3339Nano) + `"}`
			req := httptest.NewRequest(http.MethodPost, "/v1/snapshots/sign", strings.NewReader(body))
			req.Header.Set("Authorization", "Bearer agent-token")
			res := httptest.NewRecorder()
			s.signSnapshot(res, req)
			if res.Code != test.want {
				t.Fatalf("snapshot sign returned %d, want %d", res.Code, test.want)
			}
		})
	}
}

func snapshotServer(t *testing.T) server {
	t.Helper()
	s := boundServer(t, identity{
		SubjectID: fixtureUserID, IdPUsername: "ldap", Team: "examples",
		Cell: "cell-eaws-lh1", Incarnation: "eaws-lh1",
		Lineage: "ldap-dev", IsRoot: true, Machine: "ldap-dev", Workspace: "dev",
		WorkspaceVolume: "/var/lib/workspace",
	}, &fixedCoordinator{})
	s.snapshotRootKey = []byte("0123456789abcdef0123456789abcdef")
	s.snapshotNow = func() time.Time {
		return time.Date(2026, 9, 3, 10, 15, 30, 0, time.UTC)
	}
	return s
}

func fixtureSnapshotClaim() snapshotClaim {
	claim := snapshotClaim{
		Schema: 3, Selector: "manifest-1", OwnerID: fixtureUserID,
		PrincipalID: snapshotPrincipalID("https://dex.unit.test", "fixture-subject"),
		Team:        "examples", Cell: "cell-eaws-lh1", Incarnation: "eaws-lh1",
		Lineage: "ldap-dev", IsRoot: true, Workspace: "dev", SourceHost: "ldap-dev",
		SourceUser: "ldap", SourcePath: "/var/lib/workspace",
		Time: "2026-09-03T10:15:30Z",
	}
	snapshotTime, _ := time.Parse(time.RFC3339Nano, claim.Time)
	claim.StorageKey = snapshotClaimStorageKey(snapshotTime, claim.Selector)
	return claim
}

func TestSnapshotClaimAncestryValidation(t *testing.T) {
	key := make([]byte, snapshotClaimKeySize)

	// Canonical lineage format
	claim := fixtureSnapshotClaim()
	claim.Lineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	if _, err := signSnapshotClaim(key, claim); err != nil {
		t.Fatalf("canonical lineage was rejected: %v", err)
	}

	// Root claim with parent lineage must fail
	claim = fixtureSnapshotClaim()
	claim.IsRoot = true
	claim.ParentLineage = "parent-lineage"
	if _, err := signSnapshotClaim(key, claim); err == nil {
		t.Fatal("root claim with parent lineage was accepted")
	}

	// Non-root claim without parent lineage must fail
	claim = fixtureSnapshotClaim()
	claim.IsRoot = false
	claim.ParentLineage = ""
	claim.ParentSnapshot = "parent-snapshot"
	if _, err := signSnapshotClaim(key, claim); err == nil {
		t.Fatal("non-root claim without parent lineage was accepted")
	}

	// Non-root claim with valid parent lineage and snapshot must succeed
	claim = fixtureSnapshotClaim()
	claim.IsRoot = false
	claim.ParentLineage = "0780dd84-e91d-4ea2-ad24-5287129f1ed4-1726488000"
	claim.ParentSnapshot = "manifest-parent-1"
	if _, err := signSnapshotClaim(key, claim); err != nil {
		t.Fatalf("valid non-root claim was rejected: %v", err)
	}
}
