// Runs an owner-scoped rclone S3 proxy.

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"
)

const (
	rcloneBinary     = "/usr/local/bin/rclone"
	terminationGrace = 5 * time.Second
)

type runtimeOptions struct {
	command          func([]string) *exec.Cmd
	terminationGrace time.Duration
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGHUP, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if len(os.Args) == 1 {
		fmt.Fprintln(os.Stderr, "rclone arguments were not supplied")
		os.Exit(2)
	}

	options := runtimeOptions{
		command: func(arguments []string) *exec.Cmd {
			return exec.Command(rcloneBinary, arguments...)
		},
		terminationGrace: terminationGrace,
	}
	if exitCode := run(ctx, os.Args[1:], options); exitCode != 0 {
		os.Exit(exitCode)
	}
}

func startBackupCommand(arguments []string, options runtimeOptions) (*exec.Cmd, chan error, error) {
	command := options.command(arguments)
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

func run(ctx context.Context, arguments []string, options runtimeOptions) int {
	command, waited, err := startBackupCommand(arguments, options)
	if err != nil {
		fmt.Fprintln(os.Stderr, "could not start the backup listener")
		return 1
	}
	select {
	case err := <-waited:
		return processExitCode(err)
	case <-ctx.Done():
		stopProcess(command, waited, options.terminationGrace)
		return 1
	}
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
