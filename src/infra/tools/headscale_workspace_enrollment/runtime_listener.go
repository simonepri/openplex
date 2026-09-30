// Isolates registered agents and validates admission requests before dispatching to Coder handlers.

package main

import (
	"context"
	"net/http"
	"sync"
	"time"
)

const (
	workspaceAPIInFlightLimit = 64
	workspaceAPIInvalidLimit  = 60
	workspaceAPIRegisteredCap = workspaceAgentBindingLimit
	workspaceAPITokenLimit    = 120
	workspaceAPIRateWindow    = time.Minute
)

type runtimeRateWindow struct {
	count   int
	started time.Time
}

type workspaceAPILimiter struct {
	inFlight   chan struct{}
	invalid    runtimeRateWindow
	mu         sync.Mutex
	now        func() time.Time
	registered map[string]runtimeRateWindow
	registry   *workspaceAgentRegistry
}

type initialWorkspaceAgentBinding struct {
	record    workspaceAgentBinding
	tokenHash string
}

type initialWorkspaceAgentBindingKey struct{}

func newWorkspaceAPILimiter(registry *workspaceAgentRegistry) *workspaceAPILimiter {
	return &workspaceAPILimiter{
		inFlight:   make(chan struct{}, workspaceAPIInFlightLimit),
		now:        time.Now,
		registered: map[string]runtimeRateWindow{},
		registry:   registry,
	}
}

func (l *workspaceAPILimiter) limit(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case l.inFlight <- struct{}{}:
			defer func() { <-l.inFlight }()
		default:
			http.Error(w, "workspace API is busy", http.StatusServiceUnavailable)
			return
		}
		token := bearerToken(r.Header.Get("Authorization"))
		if token == "" || !userID.MatchString(token) {
			l.rejectInvalid(w)
			return
		}
		tokenHash := workspaceAgentTokenHash(token)
		if l.registry == nil {
			http.Error(w, "workspace API is unavailable", http.StatusServiceUnavailable)
			return
		}
		record, err := l.registry.binding(tokenHash)
		if err != nil {
			l.rejectInvalid(w)
			return
		}
		if !l.allowRegistered(record.TokenHash) {
			http.Error(w, "workspace API rate limit exceeded", http.StatusTooManyRequests)
			return
		}
		binding := initialWorkspaceAgentBinding{record: record, tokenHash: tokenHash}
		ctx := context.WithValue(r.Context(), initialWorkspaceAgentBindingKey{}, binding)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (l *workspaceAPILimiter) rejectInvalid(w http.ResponseWriter) {
	if !l.allowInvalid() {
		http.Error(w, "workspace API rate limit exceeded", http.StatusTooManyRequests)
		return
	}
	http.Error(w, "unauthorized", http.StatusUnauthorized)
}

func (l *workspaceAPILimiter) allowInvalid() bool {
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	window, allowed := advanceRuntimeWindow(l.invalid, now, workspaceAPIInvalidLimit)
	l.invalid = window
	return allowed
}

func (l *workspaceAPILimiter) allowRegistered(tokenHash string) bool {
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	window, exists := l.registered[tokenHash]
	if !exists && len(l.registered) >= workspaceAPIRegisteredCap {
		for key, candidate := range l.registered {
			if runtimeWindowExpired(candidate, now) {
				delete(l.registered, key)
			}
		}
		if len(l.registered) >= workspaceAPIRegisteredCap {
			return false
		}
	}
	window, allowed := advanceRuntimeWindow(window, now, workspaceAPITokenLimit)
	if allowed || exists {
		l.registered[tokenHash] = window
	}
	return allowed
}

func advanceRuntimeWindow(window runtimeRateWindow, now time.Time, limit int) (runtimeRateWindow, bool) {
	if window.started.IsZero() || runtimeWindowExpired(window, now) {
		return runtimeRateWindow{count: 1, started: now}, true
	}
	if window.count >= limit {
		return window, false
	}
	window.count++
	return window, true
}

func runtimeWindowExpired(window runtimeRateWindow, now time.Time) bool {
	return now.Before(window.started) || now.Sub(window.started) >= workspaceAPIRateWindow
}

func registeredWorkspaceAgentBinding(ctx context.Context, tokenHash string) (workspaceAgentBinding, bool) {
	binding, ok := ctx.Value(initialWorkspaceAgentBindingKey{}).(initialWorkspaceAgentBinding)
	if !ok || binding.tokenHash != tokenHash {
		return workspaceAgentBinding{}, false
	}
	return binding.record, true
}
