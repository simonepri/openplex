// Persists durable owner bindings mapping Coder workspace owners to verified OIDC subjects.

package main

import (
	"errors"
	"net/url"
	"os"
	"sort"
	"strings"
	"unicode/utf8"
)

const (
	ownerBindingFileLimit = 1 << 20
	ownerBindingLimit     = 10000
	ownerIssuerLimit      = 2048
	ownerSubjectLimit     = 1024
)

type binding struct {
	Issuer            string `json:"issuer"`
	OwnerID           string `json:"ownerId"`
	PreferredUsername string `json:"preferredUsername"`
	Subject           string `json:"subject"`
}

type ownerBindingFile struct {
	Bindings []binding `json:"bindings"`
	Schema   int       `json:"schema"`
}

func (s server) storeBinding(next binding) (binding, error) {
	s.bindingsMu.Lock()
	defer s.bindingsMu.Unlock()
	if validateOwnerBinding(next) != nil {
		return binding{}, errors.New("invalid owner identity binding")
	}
	bindings, err := readBindings(s.bindingPath)
	if err != nil {
		return binding{}, err
	}
	for _, existing := range bindings {
		ownerMatch := existing.OwnerID == next.OwnerID
		subjectMatch := existing.Issuer == next.Issuer && existing.Subject == next.Subject
		usernameMatch := existing.PreferredUsername == next.PreferredUsername
		if ownerMatch || subjectMatch || usernameMatch {
			return binding{}, errors.New("identity is already bound")
		}
	}
	if len(bindings) >= ownerBindingLimit {
		return binding{}, errors.New("owner identity binding limit exceeded")
	}
	bindings = append(bindings, next)
	sort.Slice(bindings, func(i, j int) bool { return bindings[i].OwnerID < bindings[j].OwnerID })
	return next, writeBindings(s.bindingPath, bindings)
}

func (s server) bindingForOwner(ownerID string) (binding, error) {
	if !userID.MatchString(ownerID) {
		return binding{}, errors.New("invalid owner identity")
	}
	s.bindingsMu.Lock()
	defer s.bindingsMu.Unlock()
	bindings, err := readBindings(s.bindingPath)
	if err != nil {
		return binding{}, err
	}
	for _, existing := range bindings {
		if existing.OwnerID == ownerID {
			return existing, nil
		}
	}
	return binding{}, errors.New("owner identity is not bound")
}

func readBindings(path string) ([]binding, error) {
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	var state ownerBindingFile
	if decodeBoundedStrictJSON(file, ownerBindingFileLimit, &state) != nil || state.Schema != 1 ||
		len(state.Bindings) > ownerBindingLimit {
		return nil, errors.New("invalid owner identity binding state")
	}
	if validateOwnerBindings(state.Bindings) != nil {
		return nil, errors.New("invalid owner identity binding state")
	}
	return state.Bindings, nil
}

func writeBindings(path string, bindings []binding) error {
	if len(bindings) > ownerBindingLimit {
		return errors.New("owner identity binding limit exceeded")
	}
	if validateOwnerBindings(bindings) != nil {
		return errors.New("invalid owner identity binding state")
	}
	return writeDurableJSON(path, ownerBindingFile{Bindings: bindings, Schema: 1}, ownerBindingFileLimit)
}

func validateOwnerBindings(bindings []binding) error {
	owners := make(map[string]struct{}, len(bindings))
	subjects := make(map[string]struct{}, len(bindings))
	usernames := make(map[string]struct{}, len(bindings))
	for _, binding := range bindings {
		if validateOwnerBinding(binding) != nil {
			return errors.New("invalid owner identity binding")
		}
		subject := binding.Issuer + "\x00" + binding.Subject
		if _, exists := owners[binding.OwnerID]; exists {
			return errors.New("duplicate owner identity binding")
		}
		if _, exists := subjects[subject]; exists {
			return errors.New("duplicate OIDC subject binding")
		}
		if _, exists := usernames[binding.PreferredUsername]; exists {
			return errors.New("duplicate owner login binding")
		}
		owners[binding.OwnerID] = struct{}{}
		subjects[subject] = struct{}{}
		usernames[binding.PreferredUsername] = struct{}{}
	}
	return nil
}

func validateOwnerBinding(binding binding) error {
	issuer, err := url.Parse(binding.Issuer)
	if err != nil || len(binding.Issuer) > ownerIssuerLimit || issuer.Scheme != "https" || issuer.Host == "" ||
		issuer.User != nil || issuer.RawQuery != "" || issuer.Fragment != "" ||
		binding.Subject == "" || len(binding.Subject) > ownerSubjectLimit || !utf8.ValidString(binding.Subject) ||
		strings.ContainsRune(binding.Subject, '\x00') || !loginName.MatchString(binding.PreferredUsername) ||
		!userID.MatchString(binding.OwnerID) {
		return errors.New("invalid owner identity binding")
	}
	return nil
}
