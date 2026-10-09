---
globs:
  - "**/BUILD.bazel"
  - "**/BUILD"
  - "**/*.bzl"
  - "MODULE.bazel"
  - ".bazelrc*"
  - ".bazelignore"
  - "src/bazel/**/*"
---

# Bazel

## Context

- **Consult Bazel architecture and execution tier documentation**: [`src/bazel/docs/architecture.md`](src/bazel/docs/architecture.md): complete build philosophy, execution tiers, and tool taxonomy.

## Principles

- **Preserve action hermeticity**: actions must execute within sandboxes relying strictly on declared inputs and hermetic tools; never invoke unmanaged host binaries from `$PATH` or access the external network during builds.
- **Delegate target maintenance to generators**: let Gazelle generate and synchronize Go, Python, OpenTofu, and GitOps targets; run `mise run fix` to reconcile generated targets rather than editing them manually.

## Decisions

- **Colocate companion tests beside targets in BUILD files**: place companion tests (`py_test`, `go_test`, `sh_test`) immediately following their corresponding library or binary target; never cluster tests at the bottom of `BUILD.bazel`.
- **Default non-library targets to omit companion unit tests**: binaries, CLI dispatchers, packaging targets, and shell scripts do not require companion tests by default; author a companion test only when the target encapsulates non-trivial domain logic, data transformation, or branching that is not already exercised by integration suites.
- **Order BUILD file declarations into four canonical sections**: structure `BUILD.bazel` files in strictly four sequential parts: (1) package docstring, (2) alphabetically sorted `load` statements, (3) package-level configuration (such as `package_sources()`), and (4) target declarations with colocated companion tests where applicable.
- **Prohibit Bazel-in-Bazel execution shims**: never generate or invoke `bazel` commands inside a `bazel run` target or runner script (e.g., via `_render_nested_bazel_env` or subprocess shims). Validation aspects and tests must execute natively under `bazel build` and `bazel test` so Build Event Streams (BES) remain unified and unfragmented.
