// Tests strict uniqueness, bounding, and crash safety for durable owner identity bindings.

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestOwnerBindingStateRejectsMalformedAndAmbiguousRecords(t *testing.T) {
	ownerA := fixtureUserID
	ownerB := "9826ee2e-7933-4665-aef2-2393f84a0d05"
	for name, state := range map[string]string{
		"unknown outer field":   `{"schema":1,"bindings":[],"extra":true}`,
		"unknown binding field": `{"schema":1,"bindings":[{"issuer":"https://dex.example","ownerId":"` + ownerA + `","preferredUsername":"alice","subject":"subject-a","extra":true}]}`,
		"duplicate owner":       `{"schema":1,"bindings":[{"issuer":"https://dex.example","ownerId":"` + ownerA + `","preferredUsername":"alice","subject":"subject-a"},{"issuer":"https://other.example","ownerId":"` + ownerA + `","preferredUsername":"bob","subject":"subject-b"}]}`,
		"duplicate subject":     `{"schema":1,"bindings":[{"issuer":"https://dex.example","ownerId":"` + ownerA + `","preferredUsername":"alice","subject":"subject-a"},{"issuer":"https://dex.example","ownerId":"` + ownerB + `","preferredUsername":"bob","subject":"subject-a"}]}`,
		"duplicate login":       `{"schema":1,"bindings":[{"issuer":"https://dex.example","ownerId":"` + ownerA + `","preferredUsername":"alice","subject":"subject-a"},{"issuer":"https://dex.example","ownerId":"` + ownerB + `","preferredUsername":"alice","subject":"subject-b"}]}`,
		"invalid issuer":        `{"schema":1,"bindings":[{"issuer":"http://dex.example","ownerId":"` + ownerA + `","preferredUsername":"alice","subject":"subject-a"}]}`,
		"missing schema":        `{"bindings":[]}`,
		"trailing JSON":         `{"schema":1,"bindings":[]} {}`,
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "bindings.json")
			if err := os.WriteFile(path, []byte(state), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := readBindings(path); err == nil {
				t.Fatal("malformed owner binding state was accepted")
			}
		})
	}
}

func TestOwnerBindingStateRejectsOversizeFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bindings.json")
	if err := os.WriteFile(path, bytes.Repeat([]byte(" "), ownerBindingFileLimit+1), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readBindings(path); err == nil {
		t.Fatal("oversized owner binding state was accepted")
	}
}

func TestOwnerBindingWritePublishesOneDurableSchema(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bindings.json")
	first := binding{
		Issuer: "https://dex.example", OwnerID: fixtureUserID,
		PreferredUsername: "alice", Subject: "subject-a",
	}
	if err := writeBindings(path, []binding{first}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("owner binding mode = %v", info.Mode().Perm())
	}
	if err := os.WriteFile(path+".tmp", []byte("interrupted"), 0600); err != nil {
		t.Fatal(err)
	}
	bindings, err := readBindings(path)
	if err != nil || len(bindings) != 1 || bindings[0] != first {
		t.Fatalf("interrupted temporary write changed published state: %#v, %v", bindings, err)
	}
}

func TestOwnerBindingRejectsLoginCollision(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bindings.json")
	first := binding{
		Issuer: "https://dex.example", OwnerID: fixtureUserID,
		PreferredUsername: "alice", Subject: "subject-a",
	}
	if err := writeBindings(path, []binding{first}); err != nil {
		t.Fatal(err)
	}
	s := server{bindingPath: path, bindingsMu: &sync.Mutex{}}
	_, err := s.storeBinding(binding{
		Issuer: "https://dex.example", OwnerID: "9826ee2e-7933-4665-aef2-2393f84a0d05",
		PreferredUsername: "alice", Subject: "subject-b",
	})
	if err == nil {
		t.Fatal("second owner claimed an existing durable login")
	}
}
