# Architecture

## Principles

- **Enforce acyclic directional dependencies**: the dependency graph between directories is acyclic, pointing from volatile components toward stable foundations.

## Best Practices

- **Defer abstraction until three concrete use cases emerge**: do not abstract until you have three concrete use cases; two similar-looking cases often diverge later.
- **Eliminate speculative generality and unused extension points**: do not handle inputs that cannot occur or add hooks nothing uses; defensive code for impossible cases is complexity, not safety.
