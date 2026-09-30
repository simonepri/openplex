# Meta

## Context

- **Guidance tier authority**: rules across all files follow four standardized sections: `Context` (authoritative background and facts), `Principles` (must follow), `Decisions` (should follow), and `Best Practices` (may follow); within and across sections, rules are strictly ordered by descending priority from top to bottom, where earlier rules win on conflict.
- **RuleSync generation and agent context presence**: `*.rulesync.md` files are the authoritative sources from which `AGENTS.md` is generated via `mise run agent-generate`; agents already have these rules in their system context unless context compaction or truncation has dropped them, in which case read `AGENTS.md` directly.

# Rules

## Principles

- **Prefer deterministic static checks over model guidance**: whenever an invariant can be expressed as a linter, compiler check, or static analysis rule, enforce it mechanically via code; reserve prose rules and prompt guidance for semantic decisions that require human judgment.

## Decisions

- **Rule naming and authoring format**: every rule name must be an imperative verb phrase stating the direct requirement, concise enough to serve as a stable identifier, and self-descriptive without relying on ambient section headings; never use aphorisms, metaphors, or passive noun phrases.
- **Formulate rules around invariants rather than incident symptoms**: state the underlying invariant, never the incident or bug that motivated it.
- **Keep architectural rules agnostic of specific tool implementations**: keep universal philosophy platform-agnostic; framework-specific nouns belong in component contracts, not general rules.
- **Define quantifiable thresholds instead of open-ended obligations**: define an exact, verifiable standard; demands to handle every possibility produce over-engineering.
- **State core requirements directly before presenting examples**: state the requirement directly; do not rely on lists of instances in parentheses to carry the definition.
