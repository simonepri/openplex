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

# Structure

## Principles

- **Limit files to one primary concept**: each file exports or defines one main thing; helpers can coexist but stay subordinate.
- **Colocate files by feature not layer**: a directory holds one feature's code, tests, and configuration, not one layer from every component across the monorepo.
- **Name top-level directories by business domain**: the root says what the system does, not what framework it is built with; technology appears only inside a feature.

## Decisions

- **Structure file paths as semantic phrases**: a path is part of a file's name, so a child never repeats its parent; put a file where the structure implies, not where it was convenient to write.
- **Balance directory depth against file clutter**: nesting adds indirection and flatness adds clutter; split a directory when it stops telling one story, and keep a single-item level only for uniformity with siblings.

## Best Practices

- **Colocate elements that mutate together**: when one logical change forces small edits in many non-colocated places, consolidate rather than accepting the scatter.

# Architecture

## Principles

- **Enforce acyclic directional dependencies**: the dependency graph between directories is acyclic, pointing from volatile components toward stable foundations.

## Best Practices

- **Defer abstraction until three concrete use cases emerge**: do not abstract until you have three concrete use cases; two similar-looking cases often diverge later.
- **Eliminate speculative generality and unused extension points**: do not handle inputs that cannot occur or add hooks nothing uses; defensive code for impossible cases is complexity, not safety.

# Security

## Principles

- **Isolate runtime secrets from configuration**: secrets never live in source code, configuration files, test fixtures, or VCS; inject via platform identity or secret managers at runtime.
- **Scope credentials to least privilege**: grant access only to explicitly named resources and actions; avoid wildcard grants or administrative scopes.
- **Avoid shell execution with unescaped arguments**: invoke subprocesses with argument lists, never via shell interpolation (`shell=True`) with untrusted inputs.
- **Preserve safety and procedural warnings unconditionally**: never compress or omit warnings about data loss, credential exposure, or destructive operations.

## Decisions

- **Safeguard file paths against traversal**: resolve and verify paths against an allowed root directory before filesystem access; reject unvalidated path components containing `..`.
- **Redact sensitive data from logs and telemetry**: never emit tokens, authorization headers, passwords, or PII into logs, error messages, or traces.
- **Verify integrity of fetched external assets**: never pipe unverified network scripts directly into shells (`curl | sh`); verify checksums or signatures for downloaded artifacts.

## Best Practices

- **Enforce mutual authentication and encryption in transit**: use TLS and authenticated endpoints for all network communication.

# Tooling

## Context

- **Bazel**: primary engine for all building, testing, linting, formatting, and repository checks; all targets execute hermetically in isolated sandboxes with sanitized environments.
- **mise**: task runner providing developer convenience aliases (such as `mise run fix`, `mise run check`) that invoke underlying Bazel targets.
- **Tooling and static analysis architecture**: [`src/bazel/docs/architecture.md`](src/bazel/docs/architecture.md): complete build philosophy, execution tiers, and tool taxonomy.
- **BuildBuddy results guide**: when `USE_BUILDBUDDY` is `"true"` in `mise.toml`, read [`src/bazel/docs/buildbuddy.md`](src/bazel/docs/buildbuddy.md) before fetching CI logs, failed test output, timing profiles, or artifacts.

## Principles

- **Cover every file with formatting and static analysis**: every file in the repository must be covered by automated tools for formatting and static analysis; no file type escapes mechanical verification.
- **Default tooling configuration to maximum strictness**: enable all linter and compiler checks by default, opting out only rules that are proven actively detrimental or counter-productive with documented rationale.
- **Enforce all repository checks within CI gates**: a check that no gate runs does not exist: every validator is wired into CI via Bazel or deleted.
- **Eliminate orphan files unreferenced by entry points**: every file is a declared entry point or reachable from one; fixtures, scripts, and component files unreferenced by a suite, workflow, capability, or kustomization are deleted, not kept.
- **Wrap Bazel targets and hermetic tools in task aliases**: convenience tasks in `mise.toml` must delegate to Bazel targets or hermetic tools; avoid implementing complex logic or non-hermetic operations in task commands.

# Integrity

## Decisions

- **Regenerate managed artifacts from source**: never hand-edit a generated file: change its source and regenerate (`BUILD.bazel` via `mise run fix`, agent guidance via `mise run agent-generate`).
- **Protect unordered lists with paired keep-sorted directives**: mark unordered lists with paired `keep-sorted` directives.
- **Protect mirrored content with bidirectional change guards**: when content must stay in sync with content elsewhere and consolidation cannot remove duplication, mark both sides with `LINT.IfChange`/`LINT.ThenChange`.

# Git

## Decisions

- **Write imperative commit and pull request titles**: write commit subjects and pull request titles as imperative orders describing what the change does to the tree.
- **Explain PR and commit intent instead of diff walkthroughs**: say what the change does in the plainest terms that stay true; the file-by-file tour is in the diff; do not force readers to reconstruct intent.
- **Structure commit messages with problem then solution**: record what the diff cannot show: one paragraph on the problem and one on the solution, within 256 words unless there is a specific reason to go longer.
- **Respond to review bots with code or factual rebuttals**: reply to automated review findings with code changes or technical rebuttals, never pleasantries.

# Interaction

## Principles

- **Report verified facts accurately**: the text says what is true and shows how it is known; report a failure as a failure, a skipped step as skipped, and an unchecked belief as unchecked; confidence is earned by verifying.
- **State conclusions first before rationale**: answer first; conclusion at the top, support below; a reader who stops after one sentence must have the answer, not the preamble.
- **Cut unnecessary words aggressively**: length answers the question asked, not the work done to answer it; cut every word that does no work; spend no words on framing, recap, or ceremony.

## Decisions

- **Verify claims directly instead of adding caveats**: running the check is your job, not the reader's; disclose an unverified claim only when checking is genuinely blocked, and name the blocker.
- **Verify evidence thoroughly before asserting negatives**: negatives are the easiest claims to get wrong and the most expensive to act on; verify the whole scope before claiming absence.
- **Describe affirmative scope instead of cataloging exclusions**: state what a tool, component, or architecture positively owns and does; avoid cataloging non-responsibilities unless resolving active confusion.
- **Distinguish observed facts from source citations and inferences**: distinguish what was observed from test execution, what was read in sources, and what is inferred; stating all three identically is false.
- **Re-verify facts instead of reversing under challenge**: re-run the verification rather than conceding or apologizing; 'are you sure?' carries no new data, and reversing under challenge alone swaps one ungrounded claim for another.
- **Disclose uncertainty and scope limits immediately**: state uncertainty or partial scope upfront; admitting a limit only under challenge is a failed first answer.
- **Eliminate pleasantries and conversational filler**: omit greetings, apologies, emoji, praise of questions, prompt restatements, upcoming action announcements, and turn recaps.
- **Report semantic outcomes instead of mechanics or diffs**: describe what was found or changed and what it means for the consumer; do not narrate tool commands or walk through file diffs line by line.
- **Format document structure proportional to length**: a one-sentence answer is one sentence; use headers and lists only for multi-part content.
- **Quote exact failure lines instead of verbose logs**: cite the specific error line or diff hunk rather than pasting surrounding log context.
- **Execute writing standards silently without commentary**: never announce compliance with style rules; run the check and report what it found.
- **Estimate implementation ETAs proactively via optimal model tier and task parallelism**: when discussing or presenting an implementation plan or proposal, proactively provide an estimated completion time based on parallel execution; decompose the work into independent concurrent tasks, choose the fastest capable model tier for each task (preferring fast Flash models for mechanical code edits, schema definitions, and tests), size concurrency around a baseline of ~8 parallel agents (scaling up or down based on natural task boundaries), and state the planned agent count, model tiers, and critical-path wall-clock duration.

# Writing

## Principles

- **Prioritize natural unambiguous prose**: write sentences that are direct, natural, and unambiguous; clear communication supersedes mechanical stylistic heuristics whenever they conflict.
- **Prioritize clarity over explanatory comments**: structure, naming, and code make purpose obvious; comment only what refactoring cannot express; never narrate syntax or diff changes.

## Decisions

- **Maintain consistent terminology across code and documentation**: use the same term for the same thing; a reader who sees two words looks for two concepts.
- **Prefer plain everyday words over technical jargon**: prefer direct, everyday English over Latinate words or specialized jargon unless a domain term is strictly necessary; specialized vocabulary obscures meaning without adding precision.
- **Mark unfinished work with standard tags**: `TODO(owner)` for a planned improvement with an assigned owner, `FIXME(owner)` for a known bug, `HACK(owner)` for a workaround to revisit, or `NOTE(owner)` for critical non-obvious context across code, configuration, and documentation.

## Best Practices

- **Use active voice with explicit actors**: use active verbs with clear actors; say "the server closes the connection", not "the connection is closed".
- **Use strong verbs in place of nominalizations**: prefer direct verbs over smothered noun constructions ("validate", not "perform validation of"; "decide", not "make a determination").
- **Eliminate dead metaphors and corporate clichés**: eliminate stale idioms and stock phrases; if a metaphor needs a following sentence to explain what it meant, it displaced meaning instead of carrying it.
- **Use standard ASCII punctuation over stylized unicode symbols**: use straight quotes and standard hyphens; avoid decorative symbols and em-dashes; use a colon, comma, or separate sentence instead.
- **Vary sentence lengths to avoid repetitive cadence**: mix short and medium sentences in prose to avoid the uniform cadence of generated text; keep parallel structure strictly for lists and tables.
