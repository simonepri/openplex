# Security

## Principles

- **Isolate runtime secrets from configuration**: secrets never live in source code, configuration files, test fixtures, or VCS; inject via platform identity or secret managers at runtime.
- **Scope credentials to least privilege**: grant access only to explicitly named resources and actions; avoid wildcard grants or administrative scopes.
- **Avoid shell execution with unescaped arguments**: invoke subprocesses with argument lists, never via shell interpolation (`shell=True`) with untrusted inputs.
- **Preserve safety and procedural warnings unconditionally**: never compress or omit warnings about data loss, credential exposure, or destructive operations.

## Decisions

- **Require explicit AWS profile selection for cloud operations**: do not configure ambient default cloud profiles; prefix all cloud CLI, AWS, and Terraform/OpenTofu commands explicitly per execution with an `AWS_PROFILE=<profile>` statement matching repository profile definitions.
- **Provision secrets through cloud secret managers rather than Kubernetes**: never manually create, edit, or patch secrets directly inside Kubernetes clusters; all credentials must originate in authoritative cloud secret managers and flow unidirectionally into Kubernetes via automated secret synchronization.
- **Safeguard file paths against traversal**: resolve and verify paths against an allowed root directory before filesystem access; reject unvalidated path components containing `..`.
- **Redact sensitive data from logs and telemetry**: never emit tokens, authorization headers, passwords, or PII into logs, error messages, or traces.
- **Verify integrity of fetched external assets**: never pipe unverified network scripts directly into shells (`curl | sh`); verify checksums or signatures for downloaded artifacts.
- **Prohibit interactive pod execution for database queries**: never invoke `kubectl exec` into ClickHouse or other database pods to run queries; execute queries via repository tooling (`mise run //src/infra:clickhouse-query`) or read-only service endpoints.

## Best Practices

- **Enforce mutual authentication and encryption in transit**: use TLS and authenticated endpoints for all network communication.
