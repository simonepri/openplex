# Integrity

## Decisions

- **Regenerate managed artifacts from source**: never hand-edit a generated file: change its source and regenerate (`BUILD.bazel` via `mise run fix`, agent guidance via `mise run agent-generate`).
- **Protect unordered lists with paired keep-sorted directives**: mark unordered lists with paired `keep-sorted` directives.
- **Protect mirrored content with bidirectional change guards**: when content must stay in sync with content elsewhere and consolidation cannot remove duplication, mark both sides with `LINT.IfChange`/`LINT.ThenChange`.
