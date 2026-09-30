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

- **Colocate companion tests beside targets in BUILD files**: place companion tests (`py_test`, `sh_test`, `go_test`) immediately following their corresponding library or binary target; never cluster tests at the bottom of `BUILD.bazel`.
- **Enforce canonical four-part BUILD file anatomy**: structure `BUILD.bazel` files with package docstring, alphabetically sorted `load` statements, package configuration (`package_sources()`), and target/test pairs.
