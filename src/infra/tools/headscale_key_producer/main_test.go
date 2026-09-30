// Tests key-rotation and Secret handoff boundaries for the Headscale record producer.

package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const (
	fixtureAuthKey       = "hskey-auth-abcdefghijkl-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	fixtureAuthKeyDigest = "e5d4bbe550e95a4c6f0ff2059c8e89508233dad7e8907ca35beddb1255851a71"
	fixtureMaskedKey     = "hskey-auth-abcdefghijkl-***"
	rotatedAuthKey       = "hskey-auth-mnopqrstuvwx-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	rotatedAuthKeyDigest = "620da328ed25721ed93bea3e09ff08f4ba12bcd46a233ea8fefd4001eb9e5bd7"
	rotatedMaskedKey     = "hskey-auth-mnopqrstuvwx-***"
)

var fixtureHandoff = handoff{
	Version:  1,
	Provider: "floci",
	Source: handoffSource{
		Namespace:       "secret-records",
		RecordName:      "headscale-preauth-ctrl-eaws-lh1",
		SecretStoreName: "local-secret-records",
	},
	Target: handoffTarget{Namespace: "tailscale-system", SecretName: "headscale-preauth"},
}

type fakeHeadscale struct {
	keys        []preAuthKey
	created     preAuthKey
	createCalls int
}

func (f *fakeHeadscale) list(context.Context) ([]preAuthKey, error) {
	return f.keys, nil
}

func (f *fakeHeadscale) create(context.Context) (preAuthKey, error) {
	f.createCalls++
	return f.created, nil
}

func TestParseHandoffRejectsInferredOrUnknownCoordinates(t *testing.T) {
	t.Parallel()
	valid, err := json.Marshal(fixtureHandoff)
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"missing":           "",
		"unknown field":     string(valid[:len(valid)-1]) + `,"record":"guessed"}`,
		"wrong store":       strings.Replace(string(valid), `"secretStoreName":"local-secret-records"`, `"secretStoreName":"runtime-secrets"`, 1),
		"wrong target name": strings.Replace(string(valid), `"secretName":"headscale-preauth"`, `"secretName":"guessed"`, 1),
		"wrong target namespace": strings.Replace(
			string(valid), `"namespace":"tailscale-system"`, `"namespace":"tailscale"`, 1,
		),
		"wrong provider":     strings.Replace(string(valid), `"provider":"floci"`, `"provider":"unsupported"`, 1),
		"unsupported schema": strings.Replace(string(valid), `"version":1`, `"version":2`, 1),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if _, err := parseHandoff(raw); err == nil {
				t.Fatal("parseHandoff accepted an invalid contract")
			}
		})
	}
}

func TestReconcileRetainsCurrentTaggedReusableKey(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_800_000_000, 0)
	key := validKey(now.Add(30 * 24 * time.Hour))
	server, client, patches := kubernetesFixture(t, currentRecord(key))
	defer server.Close()
	headscale := &fakeHeadscale{keys: []preAuthKey{listedKey(key)}, created: validKey(now.Add(31 * 24 * time.Hour))}
	reconciler := producer{
		handoff: fixtureHandoff, kubernetes: client, headscale: headscale, now: func() time.Time { return now },
	}

	rotated, err := reconciler.reconcile(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rotated || headscale.createCalls != 0 || *patches != 0 {
		t.Fatalf("current key rotated: rotated=%v creates=%d patches=%d", rotated, headscale.createCalls, *patches)
	}
}

func TestReconcileRotatesMissingHeadscaleKeyOnce(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_800_000_000, 0)
	created := validKey(now.Add(30 * 24 * time.Hour))
	created.Key = rotatedAuthKey
	server, client, patches := kubernetesFixture(t, secret{})
	defer server.Close()
	headscale := &fakeHeadscale{created: created}
	reconciler := producer{
		handoff: fixtureHandoff, kubernetes: client, headscale: headscale, now: func() time.Time { return now },
	}

	rotated, err := reconciler.reconcile(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !rotated || headscale.createCalls != 1 || *patches != 1 {
		t.Fatalf("missing key was not rotated once: rotated=%v creates=%d patches=%d", rotated, headscale.createCalls, *patches)
	}
}

func TestReconcileRotatesCorruptedRecordKeyOnce(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_800_000_000, 0)
	listed := validKey(now.Add(30 * 24 * time.Hour))
	created := validKey(now.Add(30 * 24 * time.Hour))
	created.ID = 8
	created.Key = rotatedAuthKey
	record := currentRecord(listed)
	record.Data["authkey"] = base64.StdEncoding.EncodeToString([]byte(rotatedAuthKey))
	server, client, patches := kubernetesFixture(t, record)
	defer server.Close()
	headscale := &fakeHeadscale{keys: []preAuthKey{listedKey(listed)}, created: created}
	reconciler := producer{
		handoff: fixtureHandoff, kubernetes: client, headscale: headscale, now: func() time.Time { return now },
	}

	rotated, err := reconciler.reconcile(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !rotated || headscale.createCalls != 1 || *patches != 1 {
		t.Fatalf("corrupted key was not rotated once: rotated=%v creates=%d patches=%d", rotated, headscale.createCalls, *patches)
	}
}

func TestRecordCurrentRequiresDigestAndMaskedPrefix(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_800_000_000, 0)
	key := validKey(now.Add(30 * 24 * time.Hour))
	cases := map[string]struct {
		changeRecord func(*secret)
		changeListed func(*preAuthKey)
		want         bool
	}{
		"current": {want: true},
		"corrupted bytes": {changeRecord: func(record *secret) {
			record.Data["authkey"] = base64.StdEncoding.EncodeToString([]byte(rotatedAuthKey))
		}},
		"missing digest": {changeRecord: func(record *secret) {
			delete(record.Metadata.Annotations, keyDigestAnnotation)
		}},
		"wrong digest": {changeRecord: func(record *secret) {
			record.Metadata.Annotations[keyDigestAnnotation] = rotatedAuthKeyDigest
		}},
		"masked prefix mismatch": {changeListed: func(listed *preAuthKey) {
			listed.Key = rotatedMaskedKey
		}},
		"missing egress tag": {changeListed: func(listed *preAuthKey) {
			listed.ACLTags = []string{"tag:subnet-router"}
		}},
		"wrong egress tag": {changeListed: func(listed *preAuthKey) {
			listed.ACLTags = []string{"tag:k8s", "tag:subnet-router"}
		}},
		"tag order is not significant": {changeListed: func(listed *preAuthKey) {
			listed.ACLTags = []string{"tag:subnet-router", "tag:k8s-egress"}
		}, want: true},
	}

	for name, test := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			record := currentRecord(key)
			listed := listedKey(key)
			if test.changeRecord != nil {
				test.changeRecord(&record)
			}
			if test.changeListed != nil {
				test.changeListed(&listed)
			}
			if got := recordCurrent(record, []preAuthKey{listed}, now.Add(rotationLead)); got != test.want {
				t.Fatalf("recordCurrent() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestReconcileRetriesWhenReadinessCannotBePublished(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_800_000_000, 0)
	key := validKey(now.Add(30 * 24 * time.Hour))
	server, client, _ := kubernetesFixture(t, currentRecord(key))
	defer server.Close()
	reconciler := &producer{
		handoff:    fixtureHandoff,
		kubernetes: client,
		headscale:  &fakeHeadscale{keys: []preAuthKey{listedKey(key)}},
		now:        func() time.Time { return now },
	}

	_, err := reconcileAndReady(context.Background(), reconciler, func() error {
		return errors.New("read-only filesystem")
	})
	if err == nil || !strings.Contains(err.Error(), "publish readiness") {
		t.Fatalf("readiness failure was not retryable: %v", err)
	}
}

func TestCommandAndAPIErrorsRedactResponseMaterial(t *testing.T) {
	t.Parallel()
	const sensitive = "hskey-auth-must-not-escape"
	directory := t.TempDir()
	commandPath := filepath.Join(directory, "failing-headscale")
	if err := os.WriteFile(
		commandPath,
		[]byte("#!/bin/sh\nprintf '%s\\n' '"+sensitive+"'\nprintf '%s\\n' '"+sensitive+"' >&2\nexit 7\n"),
		0o700,
	); err != nil {
		t.Fatal(err)
	}
	commandClient := commandHeadscaleClient{binary: commandPath, config: "/unused"}
	_, commandErr := commandClient.list(context.Background())
	if commandErr == nil || strings.Contains(commandErr.Error(), sensitive) {
		t.Fatalf("command error exposed response material: %v", commandErr)
	}

	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusForbidden)
		_, _ = writer.Write([]byte(sensitive))
	}))
	defer server.Close()
	tokenPath := filepath.Join(directory, "token")
	if err := os.WriteFile(tokenPath, []byte("fixture-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	kubernetes := kubernetesClient{baseURL: server.URL, httpClient: server.Client(), tokenPath: tokenPath}
	_, apiErr := kubernetes.secret(context.Background(), "secret-records", "headscale-preauth")
	if apiErr == nil || strings.Contains(apiErr.Error(), sensitive) {
		t.Fatalf("API error exposed response material: %v", apiErr)
	}
}

func validKey(expiry time.Time) preAuthKey {
	return preAuthKey{
		ID: 7, Key: fixtureAuthKey, Reusable: true,
		Expiration: protobufTime{Seconds: expiry.Unix()}, ACLTags: requiredAuthTags,
	}
}

func listedKey(key preAuthKey) preAuthKey {
	key.Key = fixtureMaskedKey
	return key
}

func currentRecord(key preAuthKey) secret {
	return secret{
		Metadata: secretMetadata{Annotations: map[string]string{
			"example.invalid/unowned": "retained",
			keyIDAnnotation:           strconv.FormatUint(key.ID, 10),
			expiryAnnotation:          timeToEpoch(key.Expiration),
			keyDigestAnnotation:       fixtureAuthKeyDigest,
		}},
		Data: map[string]string{"authkey": base64.StdEncoding.EncodeToString([]byte(key.Key))},
	}
}

func timeToEpoch(value protobufTime) string {
	return strconv.FormatInt(value.Seconds, 10)
}

func kubernetesFixture(t *testing.T, record secret) (*httptest.Server, *kubernetesClient, *int) {
	t.Helper()
	patches := 0
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.Method {
		case http.MethodGet:
			writer.Header().Set("Content-Type", "application/json")
			if err := json.NewEncoder(writer).Encode(record); err != nil {
				t.Error(err)
			}
		case http.MethodPatch:
			patches++
			var patch map[string]any
			if err := json.NewDecoder(request.Body).Decode(&patch); err != nil {
				t.Error(err)
			}
			encoded := patch["data"].(map[string]any)["authkey"].(string)
			decoded, err := base64.StdEncoding.DecodeString(encoded)
			if err != nil {
				t.Error(err)
			}
			if string(decoded) != rotatedAuthKey {
				t.Error("producer published an unexpected key")
			}
			annotations := patch["metadata"].(map[string]any)["annotations"].(map[string]any)
			if len(annotations) != 3 || annotations[keyDigestAnnotation] != rotatedAuthKeyDigest {
				t.Error("producer did not patch only its owned integrity annotations")
			}
			writer.WriteHeader(http.StatusOK)
		default:
			writer.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("fixture-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	client := &kubernetesClient{baseURL: server.URL, httpClient: server.Client(), tokenPath: tokenPath}
	return server, client, &patches
}
