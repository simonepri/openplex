---
globs:
  - "**/*.{py,ts,tsx,js,jsx,go,rs,bzl,sh}"
---

# Architecture

## Decisions

- **Define protocol interfaces before variant types**: when the family is known up front, a declared extension point or variants fixed by an external spec, define the shared interface before variant implementations.
- **Standardize variant type structures**: when types share a role, prefer uniform structure so consumers do not need conditional logic.

## Best Practices

- **Introduce abstractions only at system boundaries**: place interfaces at system boundaries (I/O, external services, plugin points); use concrete types and functions internally.
- **Prefer immutable data structures**: create new objects instead of modifying existing ones in place.
- **Restrict member visibility to narrowest scope**: default to the narrowest visibility; talk to direct collaborators, not their internals.
- **Publish state updates atomically**: assemble state in isolation and make it visible in a single atomic step; exposing intermediate progress creates race conditions and partial reads.

# Logic

## Principles

- **Maximize signal-to-noise ratio**: remove repetition, extraneous syntax, and unnecessary abstraction, but never sacrifice clarity for brevity.
- **Make critical details visible**: avoid hiding critical control flow, side effects, or error handling in easily-overlooked places.
- **Match surrounding conventions**: language idioms > codebase patterns > team conventions > personal preference; absent specific direction, match surrounding code.

## Decisions

- **Limit functions to single responsibility**: each function does one thing well; if you need "and" to describe it, split it.
- **Maintain single abstraction level per function**: do not mix high-level intent with low-level mechanics; each function reads at a single level of the story.
- **Enforce command-query separation**: a function either does something or answers something, not both.
- **Prefer pure functions and isolate side effects**: isolate side effects; use dependency injection for external resources.
- **Select implementation language by execution model and requirements**: author system daemons, long-running processes, Kubernetes controllers/reconcilers, network/HTTP proxies, and container runtime orchestrators in Go using official typed client SDKs; author AST manipulation, monorepo linters, ML/LLM integrations, and complex data transformations in Python; author end-to-end Kubernetes cluster conformance suites in Chainsaw; validate Kubernetes manifests declaratively via schema tools rather than imperative parsing scripts.
- **Restrict Shell scripts to linear process delegation**: Shell scripts (`.sh`) are restricted to linear command execution, environment setup, and process delegation (`exec`); any logic requiring control flow branching (`if`/`case`), iteration (`for`/`while`), structured data parsing (JSON/YAML), process concurrency, or signal traps must be authored in a typed language (Python or Go).
- **Use options object for complex signatures**: use an options argument when a function has many parameters (~4+) or optional configuration.
- **Return early for edge cases and errors**: return early for edge cases and errors to keep the happy path unindented.
- **Order file contents top-down**: organize files like a newspaper, with high-level functions at the top and implementation details below; place callees below their callers.

# Naming

## Decisions

- **Choose accurate distinct names**: names must not mislead; do not call a `Map` a `list` or a partial result `result`; if two names differ, the difference must be meaningful.
- **Use verbs for actions and nouns for accessors**: use verbs for functions that perform actions; getters and accessors can drop the `get` prefix.
- **Scale identifier length to scope**: short names for small scopes, descriptive names for large scopes; prefer names distinctive enough to search for over generic ones (`data`, `result`, `value`).
- **Omit redundant context in identifiers**: names should not repeat information clear from context (`user.name()`, not `user.getUserName()`).

# Comments

## Decisions

- **Document interface contracts not implementation details**: document what the function does, not how it does it.
- **Document non-obvious parameters only**: explaining every parameter adds noise.
- **Document non-obvious concurrency semantics**: document thread-safety and async behavior when not obvious from the signature.

# Errors

## Decisions

- **Handle errors at boundaries and propagate internally**: handle errors explicitly at system boundaries (APIs, external services, user input); let internal errors propagate.
- **Add actionable context when wrapping errors**: add useful context; avoid repeating information already in the underlying error.
- **Avoid in-band error signals**: do not use special values (-1, null, empty string) to signal errors; use explicit types, optionals, or exceptions.
- **Use typed errors for programmatic handling**: use typed errors so callers handle them programmatically rather than via string matching.

## Best Practices

- **Parse inputs into typed structures at boundaries**: convert raw input to typed structures at system boundaries; work with typed data internally, and reject bad values where they enter.
- **Handle optionality and nil values explicitly**: be intentional about optionality; do not use optional chaining or null-coalescing to silently ignore unexpected nulls.

# Security

## Principles

- **Prohibit dynamic code evaluation**: never use dynamic execution primitives such as `eval()` or `exec()`; dynamic code execution introduces arbitrary execution vulnerabilities.

## Decisions

- **Parameterize external queries and commands**: never concatenate raw input into database queries, system commands, or API paths; use parameterized queries and typed SDKs.

## Best Practices

- **Enforce input limits at boundaries**: validate payload size and schema before processing to guard against memory exhaustion and algorithmic denial of service.
