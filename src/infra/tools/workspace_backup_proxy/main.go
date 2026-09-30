// Runs an owner-scoped rclone S3 proxy fenced by snapshot broker lease validation.

package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	acquireRetryInterval  = 2 * time.Second
	acquireWindow         = 15 * time.Minute
	maximumGeneration     = uint64(1<<63 - 1)
	rcloneBinary          = "/usr/local/bin/rclone"
	renewInterval         = 30 * time.Second
	requestTimeout        = 5 * time.Second
	safetyDeadline        = 90 * time.Second
	terminationGrace      = 5 * time.Second
	workspaceAgentToken   = "CODER_AGENT_TOKEN"
	workspaceBrokerURL    = "KOPIA_SNAPSHOT_BROKER_URL"
	workspaceNonceBytes   = 32
	workspaceTailnetProxy = "TAILNET_HTTP_PROXY"
)

type brokerClient struct {
	baseURL    *url.URL
	httpClient *http.Client
	token      string
}

type brokerResponseError struct {
	status int
}

func (e brokerResponseError) Error() string {
	return fmt.Sprintf("snapshot broker returned HTTP %d", e.status)
}

type lease struct {
	generation uint64
	nonce      string
}

type runtimeOptions struct {
	acquireRetry     time.Duration
	acquireWindow    time.Duration
	command          func([]string) *exec.Cmd
	renewInterval    time.Duration
	requestTimeout   time.Duration
	safetyDeadline   time.Duration
	terminationGrace time.Duration
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGHUP, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	token := os.Getenv(workspaceAgentToken)
	brokerURL := os.Getenv(workspaceBrokerURL)
	tailnetProxyURL := os.Getenv(workspaceTailnetProxy)
	if token == "" || brokerURL == "" || tailnetProxyURL == "" {
		fmt.Fprintln(os.Stderr, "workspace backup proxy identity is incomplete")
		os.Exit(2)
	}
	if len(os.Args) == 1 {
		fmt.Fprintln(os.Stderr, "rclone arguments were not supplied")
		os.Exit(2)
	}
	httpClient, err := newTailnetHTTPClient(tailnetProxyURL, requestTimeout)
	if err != nil {
		fmt.Fprintln(os.Stderr, "tailnet HTTP proxy URL is invalid")
		os.Exit(2)
	}
	client, err := newBrokerClient(brokerURL, token, httpClient)
	if err != nil {
		fmt.Fprintln(os.Stderr, "snapshot broker URL is invalid")
		os.Exit(2)
	}
	if err := os.Unsetenv(workspaceAgentToken); err != nil {
		fmt.Fprintln(os.Stderr, "could not isolate the workspace agent token")
		os.Exit(1)
	}

	options := runtimeOptions{
		acquireRetry:  acquireRetryInterval,
		acquireWindow: acquireWindow,
		command: func(arguments []string) *exec.Cmd {
			return exec.Command(rcloneBinary, arguments...)
		},
		renewInterval:    renewInterval,
		requestTimeout:   requestTimeout,
		safetyDeadline:   safetyDeadline,
		terminationGrace: terminationGrace,
	}
	if exitCode := run(ctx, client, os.Args[1:], options); exitCode != 0 {
		os.Exit(exitCode)
	}
}

func startBackupCommand(arguments []string, options runtimeOptions) (*exec.Cmd, chan error, error) {
	command := options.command(arguments)
	if command.Env == nil {
		command.Env = sanitizedEnvironment(os.Environ())
	} else {
		command.Env = sanitizedEnvironment(command.Env)
	}
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Start(); err != nil {
		return nil, nil, err
	}
	waited := make(chan error, 1)
	go func() { waited <- command.Wait() }()
	return command, waited, nil
}

func renewLease(ctx context.Context, client *brokerClient, lineageLease lease, deadline time.Time, timeout, safetyDeadline time.Duration) (time.Time, bool) {
	if !time.Now().Before(deadline) {
		return deadline, false
	}
	renewCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	err := client.renew(renewCtx, lineageLease, timeout)
	now := time.Now()
	if err != nil || !now.Before(deadline) {
		return deadline, false
	}
	return now.Add(safetyDeadline), true
}

func runSupervisionLoop(
	ctx context.Context,
	client *brokerClient,
	lineageLease lease,
	command *exec.Cmd,
	waited chan error,
	options runtimeOptions,
	release func() bool,
) int {
	renewTimer := time.NewTimer(options.renewInterval)
	deadlineTimer := time.NewTimer(options.safetyDeadline)
	defer renewTimer.Stop()
	defer deadlineTimer.Stop()
	deadline := time.Now().Add(options.safetyDeadline)

	stopAndRelease := func() int {
		stopProcess(command, waited, options.terminationGrace)
		if !release() {
			return 1
		}
		return 1
	}

	for {
		select {
		case err := <-waited:
			if !release() {
				return 1
			}
			return processExitCode(err)
		case <-ctx.Done():
			return stopAndRelease()
		case <-deadlineTimer.C:
			fmt.Fprintln(os.Stderr, "workspace lineage deadline elapsed; stopping the backup listener")
			return stopAndRelease()
		case <-renewTimer.C:
			newDeadline, ok := renewLease(ctx, client, lineageLease, deadline, options.requestTimeout, options.safetyDeadline)
			if !ok {
				fmt.Fprintln(os.Stderr, "workspace lineage renewal failed; stopping the backup listener")
				return stopAndRelease()
			}
			deadline = newDeadline
			resetTimer(deadlineTimer, options.safetyDeadline)
			resetTimer(renewTimer, options.renewInterval)
		}
	}
}

func run(ctx context.Context, client *brokerClient, arguments []string, options runtimeOptions) int {
	lineageLease, err := acquireWithRetry(ctx, client, options)
	if err != nil {
		fmt.Fprintln(os.Stderr, "could not acquire workspace lineage lease")
		return 1
	}
	leaseHeld := true
	release := func() bool {
		if !leaseHeld {
			return true
		}
		leaseHeld = false
		releaseCtx, cancel := context.WithTimeout(context.Background(), options.requestTimeout)
		defer cancel()
		if err := client.release(releaseCtx, lineageLease, options.requestTimeout); err != nil {
			fmt.Fprintln(os.Stderr, "failed to release workspace lineage lease")
			return false
		}
		return true
	}

	if ctx.Err() != nil {
		release()
		return 1
	}
	command, waited, err := startBackupCommand(arguments, options)
	if err != nil {
		fmt.Fprintln(os.Stderr, "could not start the backup listener")
		release()
		return 1
	}
	return runSupervisionLoop(ctx, client, lineageLease, command, waited, options, release)
}

func acquireWithRetry(ctx context.Context, client *brokerClient, options runtimeOptions) (lease, error) {
	nonceBytes := make([]byte, workspaceNonceBytes)
	if _, err := io.ReadFull(rand.Reader, nonceBytes); err != nil {
		return lease{}, err
	}
	candidate := lease{nonce: base64.RawURLEncoding.EncodeToString(nonceBytes)}
	acquireCtx, cancel := context.WithTimeout(ctx, options.acquireWindow)
	defer cancel()

	for {
		lineageLease, err := client.acquire(acquireCtx, candidate, options.requestTimeout)
		if err == nil {
			return lineageLease, nil
		}
		if !retryableAcquire(acquireCtx, err) {
			return lease{}, err
		}
		timer := time.NewTimer(options.acquireRetry)
		select {
		case <-acquireCtx.Done():
			timer.Stop()
			return lease{}, acquireCtx.Err()
		case <-timer.C:
		}
	}
}

func retryableAcquire(acquireCtx context.Context, err error) bool {
	if acquireCtx.Err() != nil {
		return false
	}
	var responseError brokerResponseError
	if errors.As(err, &responseError) {
		switch responseError.status {
		case http.StatusUnauthorized, http.StatusConflict, http.StatusTooManyRequests, http.StatusBadGateway,
			http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			return true
		default:
			return false
		}
	}
	return !errors.Is(err, context.Canceled)
}

func newBrokerClient(rawURL, token string, httpClient *http.Client) (*brokerClient, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "https" || parsed.Port() != "8444" ||
		!isRuntimeBrokerHostname(parsed.Hostname()) || parsed.User != nil || token == "" || httpClient == nil ||
		(parsed.Path != "" && parsed.Path != "/") || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, errors.New("broker URL must be the private tailnet runtime alias")
	}
	parsed.Path = ""
	return &brokerClient{baseURL: parsed, httpClient: httpClient, token: token}, nil
}

func isRuntimeBrokerHostname(hostname string) bool {
	if len(hostname) > 253 {
		return false
	}
	labels := strings.Split(hostname, ".")
	if len(labels) < 4 || labels[1] != "tailnet" || (labels[2] != "k8s" && labels[2] != "c") {
		return false
	}
	router := strings.TrimSuffix(labels[0], "-services")
	if router == labels[0] || !strings.HasPrefix(router, "ctrl-") {
		return false
	}
	for _, label := range append([]string{router}, labels[3:]...) {
		if !isDNSLabel(label) {
			return false
		}
	}
	return true
}

func isDNSLabel(label string) bool {
	if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
		return false
	}
	for index, character := range label {
		if (character < 'a' || character > 'z') && (character < '0' || character > '9') && character != '-' {
			return false
		}
		if character == '-' && (index == 0 || index == len(label)-1) {
			return false
		}
	}
	return true
}

func newTailnetHTTPClient(rawURL string, timeout time.Duration) (*http.Client, error) {
	proxyURL, err := url.Parse(rawURL)
	if err != nil || proxyURL.Scheme != "http" || proxyURL.Host != "127.0.0.1:1055" || proxyURL.User != nil ||
		(proxyURL.Path != "" && proxyURL.Path != "/") || proxyURL.RawQuery != "" || proxyURL.Fragment != "" {
		return nil, errors.New("tailnet HTTP proxy URL must be the pod loopback proxy")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(proxyURL)
	return &http.Client{Timeout: timeout, Transport: transport}, nil
}

func (c *brokerClient) acquire(ctx context.Context, candidate lease, timeout time.Duration) (lease, error) {
	body := []byte(fmt.Sprintf(`{"nonce":"%s"}`, candidate.nonce))
	status, contentType, response, err := c.post(ctx, timeout, "/v1/lineages/acquire", body)
	if err != nil {
		return lease{}, err
	}
	if status != http.StatusOK {
		return lease{}, brokerResponseError{status: status}
	}
	mediaType, _, err := mime.ParseMediaType(contentType)
	if err != nil || mediaType != "text/plain" {
		return lease{}, errors.New("acquire response content type is invalid")
	}
	if len(response) == 0 || len(response) > 19 || response[0] == '0' {
		return lease{}, errors.New("acquire generation is invalid")
	}
	generation, err := strconv.ParseUint(string(response), 10, 63)
	if err != nil || generation == 0 || generation > maximumGeneration || strconv.FormatUint(generation, 10) != string(response) {
		return lease{}, errors.New("acquire generation is invalid")
	}
	candidate.generation = generation
	return candidate, nil
}

func (c *brokerClient) renew(ctx context.Context, lineageLease lease, timeout time.Duration) error {
	return c.mutate(ctx, timeout, "/v1/lineages/renew", lineageLease)
}

func (c *brokerClient) release(ctx context.Context, lineageLease lease, timeout time.Duration) error {
	return c.mutate(ctx, timeout, "/v1/lineages/release", lineageLease)
}

func (c *brokerClient) mutate(ctx context.Context, timeout time.Duration, path string, lineageLease lease) error {
	body := []byte(fmt.Sprintf(`{"nonce":"%s","generation":%d}`, lineageLease.nonce, lineageLease.generation))
	status, _, response, err := c.post(ctx, timeout, path, body)
	if err != nil || status != http.StatusNoContent || len(response) != 0 {
		return errors.New("lineage mutation failed")
	}
	return nil
}

func (c *brokerClient) post(ctx context.Context, timeout time.Duration, path string, body []byte) (int, string, []byte, error) {
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	endpoint := *c.baseURL
	endpoint.Path += path
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, endpoint.String(), bytes.NewReader(body))
	if err != nil {
		return 0, "", nil, err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.httpClient.Do(request)
	if err != nil {
		return 0, "", nil, err
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, 64))
	if err != nil {
		return 0, "", nil, err
	}
	return response.StatusCode, response.Header.Get("Content-Type"), responseBody, nil
}

func sanitizedEnvironment(environment []string) []string {
	prefixes := []string{
		workspaceAgentToken + "=",
		workspaceBrokerURL + "=",
		workspaceTailnetProxy + "=",
	}
	sanitized := make([]string, 0, len(environment))
	for _, entry := range environment {
		keep := true
		for _, prefix := range prefixes {
			if strings.HasPrefix(entry, prefix) {
				keep = false
				break
			}
		}
		if keep {
			sanitized = append(sanitized, entry)
		}
	}
	return sanitized
}

func stopProcess(command *exec.Cmd, waited <-chan error, grace time.Duration) {
	if command.Process == nil {
		return
	}
	_ = command.Process.Signal(syscall.SIGTERM)
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case <-waited:
		return
	case <-timer.C:
		_ = command.Process.Kill()
		<-waited
	}
}

func processExitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) && exitError.ExitCode() >= 0 {
		return exitError.ExitCode()
	}
	return 1
}

func resetTimer(timer *time.Timer, duration time.Duration) {
	if duration < 0 {
		duration = 0
	}
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
	timer.Reset(duration)
}
