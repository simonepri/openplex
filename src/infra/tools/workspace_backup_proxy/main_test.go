// Tests broker lease fencing, rclone process supervision, and credential isolation.

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

const fixtureAgentToken = "fixture-agent-token-that-must-stay-in-the-supervisor"
const fixtureBrokerHost = "ctrl-eaws-lh1-runtime-services.tailnet.k8s.unit.test:8444"

var canonicalNonce = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)

type brokerFixture struct {
	acquireStatuses []int
	acquireFallback int
	acquireDelays   []time.Duration
	acquireStarted  chan struct{}
	renewStatus     int
	renewDelay      time.Duration

	mu                    sync.Mutex
	acquireCalls          int
	authorizationValid    bool
	nonces                []string
	releaseObservedExited bool
	rcloneAddressPath     string
}

func (f *brokerFixture) handleAcquire(response http.ResponseWriter, body []byte) {
	var document struct {
		Nonce string `json:"nonce"`
	}
	if json.Unmarshal(body, &document) != nil || !canonicalNonce.MatchString(document.Nonce) {
		response.WriteHeader(http.StatusBadRequest)
		return
	}
	f.nonces = append(f.nonces, document.Nonce)
	status := http.StatusOK
	delay := time.Duration(0)
	if f.acquireCalls < len(f.acquireStatuses) {
		status = f.acquireStatuses[f.acquireCalls]
	} else if f.acquireFallback != 0 {
		status = f.acquireFallback
	}
	if f.acquireCalls < len(f.acquireDelays) {
		delay = f.acquireDelays[f.acquireCalls]
	}
	f.acquireCalls++
	if f.acquireStarted != nil {
		select {
		case f.acquireStarted <- struct{}{}:
		default:
		}
	}
	f.mu.Unlock()
	time.Sleep(delay)
	f.mu.Lock()
	if status != http.StatusOK {
		response.WriteHeader(status)
		return
	}
	response.Header().Set("Content-Type", "text/plain; charset=utf-8")
	response.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(response, "7")
}

func (f *brokerFixture) handleRenew(response http.ResponseWriter) {
	delay := f.renewDelay
	status := f.renewStatus
	if status == 0 {
		status = http.StatusNoContent
	}
	f.mu.Unlock()
	if f.rcloneAddressPath != "" {
		deadline := time.Now().Add(time.Second)
		for {
			if _, err := os.Stat(f.rcloneAddressPath); err == nil || time.Now().After(deadline) {
				break
			}
			time.Sleep(time.Millisecond)
		}
	}
	time.Sleep(delay)
	f.mu.Lock()
	response.WriteHeader(status)
}

func (f *brokerFixture) handleRelease(response http.ResponseWriter) {
	addressBytes, err := os.ReadFile(f.rcloneAddressPath)
	if err == nil {
		listener, listenErr := net.Listen("tcp", string(addressBytes))
		if listenErr == nil {
			_ = listener.Close()
			connection, dialErr := net.DialTimeout("tcp", string(addressBytes), 20*time.Millisecond)
			if connection != nil {
				_ = connection.Close()
			}
			f.releaseObservedExited = dialErr != nil
		}
	}
	response.WriteHeader(http.StatusNoContent)
}

func (f *brokerFixture) serveHTTP(response http.ResponseWriter, request *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.authorizationValid = f.authorizationValid && request.Header.Get("Authorization") == "Bearer "+fixtureAgentToken
	if request.Method != http.MethodPost || request.Header.Get("Content-Type") != "application/json" {
		response.WriteHeader(http.StatusBadRequest)
		return
	}
	body, err := io.ReadAll(io.LimitReader(request.Body, 256))
	if err != nil {
		response.WriteHeader(http.StatusBadRequest)
		return
	}

	switch request.URL.Path {
	case "/v1/lineages/acquire":
		f.handleAcquire(response, body)
	case "/v1/lineages/renew":
		f.handleRenew(response)
	case "/v1/lineages/release":
		f.handleRelease(response)
	default:
		response.WriteHeader(http.StatusNotFound)
	}
}

func (f *brokerFixture) snapshot() (int, bool, []string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.acquireCalls, f.authorizationValid, append([]string(nil), f.nonces...), f.releaseObservedExited
}

func TestRunWaitsForAuthenticatedLeaseBeforeRclone(t *testing.T) {
	testDirectory := t.TempDir()
	fixture := &brokerFixture{
		acquireStatuses:    []int{http.StatusUnauthorized, http.StatusConflict, http.StatusServiceUnavailable, http.StatusOK},
		authorizationValid: true,
		rcloneAddressPath:  filepath.Join(testDirectory, "rclone.address"),
	}
	server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
	defer server.Close()
	client := testBrokerClient(t, server)

	t.Setenv(workspaceAgentToken, fixtureAgentToken)
	t.Setenv(workspaceBrokerURL, "https://"+fixtureBrokerHost)
	t.Setenv(workspaceTailnetProxy, "http://127.0.0.1:1055")
	exitCode := run(context.Background(), client, []string{"serve", "s3", "owner:"}, testOptions(t, testDirectory, "exit"))
	if exitCode != 0 {
		t.Fatalf("run returned %d, want 0", exitCode)
	}
	acquireCalls, authorizationValid, nonces, releaseObservedExited := fixture.snapshot()
	if acquireCalls != 4 {
		t.Fatalf("acquire calls = %d, want 4", acquireCalls)
	}
	if !authorizationValid {
		t.Fatal("broker did not receive the in-memory agent credential")
	}
	if len(nonces) != 4 || nonces[0] != nonces[1] || nonces[1] != nonces[2] || nonces[2] != nonces[3] {
		t.Fatalf("acquire retries used different nonces: %q", nonces)
	}
	if !releaseObservedExited {
		t.Fatal("lease was released before rclone exited")
	}
	assertRcloneCredentialIsolation(t, testDirectory)
}

func TestRunBoundsLiveWriterWaitWithoutStartingRclone(t *testing.T) {
	testDirectory := t.TempDir()
	fixture := &brokerFixture{
		acquireStatuses:    []int{http.StatusConflict},
		acquireFallback:    http.StatusConflict,
		authorizationValid: true,
		rcloneAddressPath:  filepath.Join(testDirectory, "rclone.address"),
	}
	server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
	defer server.Close()
	client := testBrokerClient(t, server)

	options := testOptions(t, testDirectory, "wait")
	options.acquireWindow = 35 * time.Millisecond
	exitCode := run(context.Background(), client, []string{"serve", "s3", "owner:"}, options)
	if exitCode != 1 {
		t.Fatalf("run returned %d, want 1", exitCode)
	}
	acquireCalls, _, _, _ := fixture.snapshot()
	if acquireCalls < 2 {
		t.Fatalf("conflicting lease was not retried: %d calls", acquireCalls)
	}
	if _, err := os.Stat(fixture.rcloneAddressPath); !os.IsNotExist(err) {
		t.Fatal("rclone started before acquiring an uncontested lease")
	}
}

func TestRunBoundsUnavailableRegistration(t *testing.T) {
	testDirectory := t.TempDir()
	fixture := &brokerFixture{
		acquireStatuses:    []int{http.StatusUnauthorized},
		acquireFallback:    http.StatusUnauthorized,
		authorizationValid: true,
		rcloneAddressPath:  filepath.Join(testDirectory, "rclone.address"),
	}
	server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
	defer server.Close()
	client := testBrokerClient(t, server)
	options := testOptions(t, testDirectory, "wait")
	options.acquireRetry = 5 * time.Millisecond
	options.acquireWindow = 35 * time.Millisecond

	exitCode := run(context.Background(), client, []string{"serve", "s3", "owner:"}, options)
	if exitCode != 1 {
		t.Fatalf("run returned %d, want 1", exitCode)
	}
	acquireCalls, _, _, _ := fixture.snapshot()
	if acquireCalls < 2 {
		t.Fatalf("temporary authentication failure was not retried: %d calls", acquireCalls)
	}
	if _, err := os.Stat(fixture.rcloneAddressPath); !os.IsNotExist(err) {
		t.Fatal("rclone started while registration stayed unavailable")
	}
}

func TestRunRetriesOneTimedOutAcquireAttempt(t *testing.T) {
	testDirectory := t.TempDir()
	fixture := &brokerFixture{
		acquireDelays:      []time.Duration{300 * time.Millisecond, 0},
		authorizationValid: true,
		rcloneAddressPath:  filepath.Join(testDirectory, "rclone.address"),
	}
	server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
	defer server.Close()
	client := testBrokerClient(t, server)
	options := testOptions(t, testDirectory, "exit")
	options.acquireRetry = time.Millisecond
	options.acquireWindow = 2 * time.Second
	options.requestTimeout = 100 * time.Millisecond

	exitCode := run(context.Background(), client, []string{"serve", "s3", "owner:"}, options)
	if exitCode != 0 {
		t.Fatalf("run returned %d, want 0", exitCode)
	}
	acquireCalls, _, _, _ := fixture.snapshot()
	if acquireCalls < 2 {
		t.Fatalf("per-attempt timeout ended acquisition after %d call", acquireCalls)
	}
}

func TestAcquireStopsWhenOuterContextIsCanceled(t *testing.T) {
	fixture := &brokerFixture{
		acquireDelays:      []time.Duration{50 * time.Millisecond},
		acquireStarted:     make(chan struct{}, 1),
		authorizationValid: true,
	}
	server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
	defer server.Close()
	client := testBrokerClient(t, server)
	options := testOptions(t, t.TempDir(), "exit")
	options.acquireRetry = time.Millisecond
	options.acquireWindow = time.Second
	options.requestTimeout = 100 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	completed := make(chan error, 1)
	go func() {
		_, err := acquireWithRetry(ctx, client, options)
		completed <- err
	}()
	<-fixture.acquireStarted
	cancel()

	if err := <-completed; !errors.Is(err, context.Canceled) {
		t.Fatalf("acquire error = %v, want context canceled", err)
	}
	acquireCalls, _, _, _ := fixture.snapshot()
	if acquireCalls != 1 {
		t.Fatalf("outer cancellation made %d acquire calls, want 1", acquireCalls)
	}
}

func TestRunKillsListenerBeforeReleaseAfterRenewalFailure(t *testing.T) {
	for _, testCase := range []struct {
		name string
		mode string
	}{
		{name: "cooperative", mode: "wait"},
		{name: "ignores termination", mode: "ignore-term"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			testDirectory := t.TempDir()
			fixture := &brokerFixture{
				renewStatus:        http.StatusServiceUnavailable,
				authorizationValid: true,
				rcloneAddressPath:  filepath.Join(testDirectory, "rclone.address"),
			}
			server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
			defer server.Close()
			client := testBrokerClient(t, server)

			exitCode := run(context.Background(), client, []string{"serve", "s3", "owner:"}, testOptions(t, testDirectory, testCase.mode))
			if exitCode != 1 {
				t.Fatalf("run returned %d, want 1", exitCode)
			}
			_, _, _, releaseObservedExited := fixture.snapshot()
			if !releaseObservedExited {
				t.Fatal("lease was released while rclone was still running")
			}
		})
	}
}

func TestRunRejectsRenewalCompletedPastSafetyDeadline(t *testing.T) {
	testDirectory := t.TempDir()
	fixture := &brokerFixture{
		renewDelay:         45 * time.Millisecond,
		authorizationValid: true,
		rcloneAddressPath:  filepath.Join(testDirectory, "rclone.address"),
	}
	server := httptest.NewTLSServer(http.HandlerFunc(fixture.serveHTTP))
	defer server.Close()
	client := testBrokerClient(t, server)
	options := testOptions(t, testDirectory, "wait")
	options.requestTimeout = 100 * time.Millisecond
	options.safetyDeadline = 30 * time.Millisecond

	exitCode := run(context.Background(), client, []string{"serve", "s3", "owner:"}, options)
	if exitCode != 1 {
		t.Fatalf("run returned %d, want 1", exitCode)
	}
	_, _, _, releaseObservedExited := fixture.snapshot()
	if !releaseObservedExited {
		t.Fatal("late renewal released the lease while rclone was running")
	}
}

func TestAcquireGenerationContract(t *testing.T) {
	for _, testCase := range []struct {
		name        string
		contentType string
		body        string
		accepted    bool
	}{
		{name: "plain", contentType: "text/plain", body: "1", accepted: true},
		{name: "charset", contentType: "text/plain; charset=utf-8", body: "9223372036854775807", accepted: true},
		{name: "empty", contentType: "text/plain", body: ""},
		{name: "zero", contentType: "text/plain", body: "0"},
		{name: "leading zero", contentType: "text/plain", body: "01"},
		{name: "newline", contentType: "text/plain", body: "7\n"},
		{name: "overflow", contentType: "text/plain", body: "9223372036854775808"},
		{name: "wrong type", contentType: "application/json", body: "7"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewTLSServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
				response.Header().Set("Content-Type", testCase.contentType)
				_, _ = io.WriteString(response, testCase.body)
			}))
			defer server.Close()
			client := testBrokerClient(t, server)
			_, err := client.acquire(context.Background(), lease{nonce: strings.Repeat("a", 43)}, time.Second)
			if testCase.accepted && err != nil {
				t.Fatalf("valid generation rejected: %v", err)
			}
			if !testCase.accepted && err == nil {
				t.Fatal("invalid generation accepted")
			}
		})
	}
}

func TestRcloneHelperProcess(t *testing.T) {
	if os.Getenv("TEST_RCLONE_HELPER") != "1" {
		return
	}
	directory := os.Getenv("TEST_RCLONE_HELPER_DIRECTORY")
	if directory == "" {
		os.Exit(90)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		os.Exit(91)
	}
	defer listener.Close()
	if err := os.WriteFile(filepath.Join(directory, "rclone.address"), []byte(listener.Addr().String()), 0o600); err != nil {
		os.Exit(91)
	}
	arguments := strings.Join(os.Args, "\x00")
	tokenState := "absent"
	if os.Getenv(workspaceAgentToken) != "" {
		tokenState = "present"
	}
	proxyState := "absent"
	if os.Getenv(workspaceTailnetProxy) != "" {
		proxyState = "present"
	}
	brokerState := "absent"
	if os.Getenv(workspaceBrokerURL) != "" {
		brokerState = "present"
	}
	observed := arguments + "\nagent-token=" + tokenState + "\nbroker-url=" + brokerState + "\ntailnet-proxy=" + proxyState
	if err := os.WriteFile(filepath.Join(directory, "rclone.observed"), []byte(observed), 0o600); err != nil {
		os.Exit(92)
	}
	switch os.Getenv("TEST_RCLONE_HELPER_MODE") {
	case "exit":
		return
	case "wait":
		signals := make(chan os.Signal, 1)
		signal.Notify(signals, syscall.SIGTERM)
		<-signals
		return
	case "ignore-term":
		signal.Ignore(syscall.SIGTERM)
		for {
			time.Sleep(time.Hour)
		}
	default:
		os.Exit(93)
	}
}

func TestBrokerURLIsInternalOnly(t *testing.T) {
	for _, rawURL := range []string{
		"https://headscale.ctrl-eaws-lh1.k8s.unit.test",
		"https://headscale-workspace-runtime.headscale.svc.cluster.local:8444/v1/lineages",
		"https://cell-eaws-lh1-runtime-services.tailnet.k8s.unit.test:8444",
		"https://ctrl-eaws-lh1-runtime-services.tailnet.k8s.unit.test",
		"http://" + fixtureBrokerHost,
		"https://token@" + fixtureBrokerHost,
	} {
		if _, err := newBrokerClient(rawURL, fixtureAgentToken, http.DefaultClient); err == nil {
			t.Fatalf("non-internal broker URL accepted: %s", rawURL)
		}
	}
	if _, err := newBrokerClient("https://"+fixtureBrokerHost, fixtureAgentToken, http.DefaultClient); err != nil {
		t.Fatalf("internal broker URL rejected: %v", err)
	}
	if _, err := newBrokerClient("https://ctrl-eaws-lh1-runtime-services.tailnet.c.unit.test:8444", fixtureAgentToken, http.DefaultClient); err != nil {
		t.Fatalf("internal c-domain broker URL rejected: %v", err)
	}
}

func TestTailnetHTTPProxyIsPodLoopbackOnly(t *testing.T) {
	for _, rawURL := range []string{
		"https://127.0.0.1:1055",
		"http://127.0.0.1:8080",
		"http://localhost:1055",
		"http://token@127.0.0.1:1055",
	} {
		if _, err := newTailnetHTTPClient(rawURL, time.Second); err == nil {
			t.Fatalf("non-loopback tailnet proxy URL accepted: %s", rawURL)
		}
	}
	if _, err := newTailnetHTTPClient("http://127.0.0.1:1055", time.Second); err != nil {
		t.Fatalf("pod-loopback tailnet proxy URL rejected: %v", err)
	}
}

func testBrokerClient(t *testing.T, server *httptest.Server) *brokerClient {
	t.Helper()
	parsed, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	return &brokerClient{baseURL: parsed, httpClient: server.Client(), token: fixtureAgentToken}
}

func testOptions(t *testing.T, directory, mode string) runtimeOptions {
	t.Helper()
	return runtimeOptions{
		acquireRetry:  5 * time.Millisecond,
		acquireWindow: 250 * time.Millisecond,
		command: func(arguments []string) *exec.Cmd {
			commandArguments := append([]string{"-test.run=^TestRcloneHelperProcess$", "--"}, arguments...)
			command := exec.Command(os.Args[0], commandArguments...)
			command.Env = append(os.Environ(),
				"TEST_RCLONE_HELPER=1",
				"TEST_RCLONE_HELPER_DIRECTORY="+directory,
				"TEST_RCLONE_HELPER_MODE="+mode,
			)
			return command
		},
		renewInterval:    10 * time.Millisecond,
		requestTimeout:   50 * time.Millisecond,
		safetyDeadline:   100 * time.Millisecond,
		terminationGrace: 15 * time.Millisecond,
	}
}

func assertRcloneCredentialIsolation(t *testing.T, directory string) {
	t.Helper()
	observed, err := os.ReadFile(filepath.Join(directory, "rclone.observed"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(observed), "agent-token=absent") {
		t.Fatalf("rclone inherited the agent credential: %s", observed)
	}
	if !strings.Contains(string(observed), "tailnet-proxy=absent") {
		t.Fatalf("rclone inherited the private broker transport: %s", observed)
	}
	if !strings.Contains(string(observed), "broker-url=absent") {
		t.Fatalf("rclone inherited the private broker URL: %s", observed)
	}
	if strings.Contains(string(observed), fixtureAgentToken) {
		t.Fatal("rclone received the agent credential in argv")
	}
	err = filepath.Walk(directory, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil || info.IsDir() {
			return walkErr
		}
		contents, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		if strings.Contains(string(contents), fixtureAgentToken) {
			return fmt.Errorf("agent credential was written to %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
