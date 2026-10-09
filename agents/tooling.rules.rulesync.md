# Tooling

## Context

- **Bazel**: primary engine for all building, testing, linting, formatting, and repository checks; all targets execute hermetically in isolated sandboxes with sanitized environments.
- **mise**: task runner providing developer convenience aliases (such as `mise run fix`, `mise run test`, `mise run build`) that invoke underlying Bazel targets.
- **Tooling and static analysis architecture**: [`src/bazel/docs/architecture.md`](src/bazel/docs/architecture.md): complete build philosophy, execution tiers, and tool taxonomy.
- **BuildBuddy results guide**: read [`src/bazel/docs/buildbuddy.md`](src/bazel/docs/buildbuddy.md) before fetching CI logs, failed test output, timing profiles, or artifacts.

## Principles

- **Cover every file with formatting and static analysis**: every file in the repository must be covered by automated tools for formatting and static analysis; no file type escapes mechanical verification.
- **Default tooling configuration to maximum strictness**: enable all linter and compiler checks by default, opting out only rules that are proven actively detrimental or counter-productive with documented rationale.
- **Configure policy exemptions at ingestion boundaries**: configure security, license, and linter exemptions at the external boundary of ingestion (such as repository source, upstream image digest, or package namespace), never by enumerating individual transitive dependencies or internal component files.
- **Enforce all repository checks within CI gates**: a check that no gate runs does not exist: every validator is wired into CI via Bazel or deleted.
- **Eliminate orphan files unreferenced by entry points**: every file is a declared entry point or reachable from one; fixtures, scripts, and component files unreferenced by a suite, workflow, capability, or kustomization are deleted, not kept.
- **Wrap Bazel targets and hermetic tools in task aliases**: convenience tasks in `mise.toml` must delegate to Bazel targets or hermetic tools; avoid implementing complex logic or non-hermetic operations in task commands.
- **Keep task runners strictly as transparent dispatchers**: convenience commands in `mise.toml` or CI workflows must cleanly forward user flags and attach the top-level executor directly to the user's terminal TTY; avoid intermediate wrapper scripts that capture, swallow, or proxy progress output and exit codes.
