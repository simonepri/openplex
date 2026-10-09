# Architecture

## Principles

- **Enforce acyclic directional dependencies**: the dependency graph between directories is acyclic, pointing from volatile components toward stable foundations.
- **Decouple selector tools from executor tools**: tools that resolve or filter targets, files, or tasks based on workspace state must only compute and emit identifiers to stdout; they must never execute the downstream command or manage its child process. Composition happens via pipes or outer invocation (e.g. `set -- $(resolver); executor "$@"`).
- **Prohibit recursive and nested orchestrators**: never invoke a root orchestrator or build engine (such as Bazel, Docker, or Terraform) from inside an action, runner script, or target executed by that same engine. Nested orchestration breaks telemetry streaming, fragments process locks, requires artificial environment shims, and hides exit codes.

## Best Practices

- **Defer abstraction until three concrete use cases emerge**: do not abstract until you have three concrete use cases; two similar-looking cases often diverge later.
- **Eliminate speculative generality and unused extension points**: do not handle inputs that cannot occur or add hooks nothing uses; defensive code for impossible cases is complexity, not safety.
