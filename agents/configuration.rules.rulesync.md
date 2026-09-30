---
globs:
  - "**/*.json"
  - "**/*.jsonc"
  - "**/*.yaml"
  - "**/*.yml"
  - "**/*.toml"
  - "**/*.hcl"
  - "**/*.env"
  - "**/MODULE.bazel*"
---

# Configuration

## Principles

- **Separate deploy-time configuration from code**: one artifact serves every environment through configuration, never through environment-conditional code; a value identical everywhere is code, not config.
- **Avoid speculative configuration options**: add an option only where deployments genuinely differ; each one multiplies states to test, so prefer a solid default over a choice.

## Decisions

- **Restrict configuration variation to explicit contracts**: expose what a deployment may override as a small explicit contract and vary only through it; avoid patching base internals directly.
- **Keep configuration files purely declarative**: a config file carries values; branches and loops make it a program in the wrong language, so move logic into real code beside it.

# Dependencies

## Principles

- **Track external dependencies through automated update managers**: declare all tools, container images, actions, and library packages in manifests tracked by automated dependency managers (such as Renovate); never hardcode untracked URLs or unpinned mutable tags.
- **Audit third-party dependencies before adoption**: prefer the standard library or established pinned libraries over introducing unvetted external dependencies; an external package is an ongoing maintenance and supply-chain commitment.
- **Audit dependency changelogs and release notes for breaking changes**: inspect embedded release notes, commit logs, and changelogs on automated dependency updates or bot pull requests; verify that callers, schemas, and configurations remain compatible with the new version.
- **Enforce SemVer breaking rules on pre-1.0 dependencies**: treat minor bumps in `0.x` packages as potentially breaking; only patch bumps (`0.x.y` to `0.x.(y+1)`) are backwards-compatible.

## Decisions

- **Pin new dependencies to latest stable release**: introduce new dependencies pinned to their latest stable release; pin older versions only when constrained by an explicit, documented compatibility requirement.
- **Synchronize related dependency versions**: when dependencies must upgrade together, group them in the dependency manager and guard them with change guards so partial updates cannot land silently.
- **Approve safe dependency updates to enable automerge**: if dependency changelogs show backward-compatible changes or routine patch/digest maintenance and CI checks pass, emit LGTM to permit automated merging.
