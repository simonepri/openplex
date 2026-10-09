// Tests the owner-scoped rclone S3 proxy execution and process supervision.

package main

import (
	"context"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

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
	if err := os.WriteFile(filepath.Join(directory, "rclone.observed"), []byte(arguments), 0o600); err != nil {
		os.Exit(92)
	}
	switch os.Getenv("TEST_RCLONE_HELPER_MODE") {
	case "exit":
		return
	case "exit-error":
		os.Exit(42)
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

func testOptions(t *testing.T, directory, mode string) runtimeOptions {
	t.Helper()
	return runtimeOptions{
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
		terminationGrace: 100 * time.Millisecond,
	}
}

func TestRunSupervisesSuccessfulCommand(t *testing.T) {
	testDirectory := t.TempDir()
	options := testOptions(t, testDirectory, "exit")
	exitCode := run(context.Background(), []string{"serve", "s3", "owner:"}, options)
	if exitCode != 0 {
		t.Fatalf("run returned %d, want 0", exitCode)
	}
	observed, err := os.ReadFile(filepath.Join(testDirectory, "rclone.observed"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(observed), "serve") {
		t.Fatalf("rclone did not receive arguments: %s", observed)
	}
}

func TestRunPropagatesNonZeroExitCode(t *testing.T) {
	testDirectory := t.TempDir()
	options := testOptions(t, testDirectory, "exit-error")
	exitCode := run(context.Background(), []string{"serve", "s3"}, options)
	if exitCode != 42 {
		t.Fatalf("run returned %d, want 42", exitCode)
	}
}

func TestRunHandlesContextCancellation(t *testing.T) {
	testDirectory := t.TempDir()
	options := testOptions(t, testDirectory, "wait")
	ctx, cancel := context.WithCancel(context.Background())

	go func() {
		for range 50 {
			if _, err := os.Stat(filepath.Join(testDirectory, "rclone.address")); err == nil {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
		cancel()
	}()

	exitCode := run(ctx, []string{"serve", "s3"}, options)
	if exitCode != 1 {
		t.Fatalf("run returned %d, want 1", exitCode)
	}
}

func TestRunHandlesIgnoredTermSignalWithKill(t *testing.T) {
	testDirectory := t.TempDir()
	options := testOptions(t, testDirectory, "ignore-term")
	options.terminationGrace = 20 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())

	go func() {
		for range 50 {
			if _, err := os.Stat(filepath.Join(testDirectory, "rclone.address")); err == nil {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
		cancel()
	}()

	exitCode := run(ctx, []string{"serve", "s3"}, options)
	if exitCode != 1 {
		t.Fatalf("run returned %d, want 1", exitCode)
	}
}

func TestRunFailsWhenCommandCannotStart(t *testing.T) {
	options := runtimeOptions{
		command: func(arguments []string) *exec.Cmd {
			return exec.Command("/nonexistent/binary/path")
		},
		terminationGrace: time.Millisecond,
	}
	exitCode := run(context.Background(), []string{"serve"}, options)
	if exitCode != 1 {
		t.Fatalf("run returned %d, want 1", exitCode)
	}
}
