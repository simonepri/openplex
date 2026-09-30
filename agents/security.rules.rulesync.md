# Security

## Principles

- **Isolate runtime secrets from configuration**: secrets never live in source code, configuration files, test fixtures, or VCS; inject via platform identity or secret managers at runtime.
- **Scope credentials to least privilege**: grant access only to explicitly named resources and actions; avoid wildcard grants or administrative scopes.
- **Avoid shell execution with unescaped arguments**: invoke subprocesses with argument lists, never via shell interpolation (`shell=True`) with untrusted inputs.
- **Preserve safety and procedural warnings unconditionally**: never compress or omit warnings about data loss, credential exposure, or destructive operations.

## Decisions

- **Safeguard file paths against traversal**: resolve and verify paths against an allowed root directory before filesystem access; reject unvalidated path components containing `..`.
- **Redact sensitive data from logs and telemetry**: never emit tokens, authorization headers, passwords, or PII into logs, error messages, or traces.
- **Verify integrity of fetched external assets**: never pipe unverified network scripts directly into shells (`curl | sh`); verify checksums or signatures for downloaded artifacts.

## Best Practices

- **Enforce mutual authentication and encryption in transit**: use TLS and authenticated endpoints for all network communication.
