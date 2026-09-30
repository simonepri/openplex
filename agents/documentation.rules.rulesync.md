---
globs:
  - "**/*.md"
  - "**/*.mdx"
  - "**/*.rst"
  - "**/*.txt"
---

# Documentation

## Principles

- **Justify technical documentation by enduring necessity**: each page is a promise to keep it true, so write only what protects understanding: why the system is shaped as it is, how its parts relate, and the contracts between them; delete pages that stop earning their keep.
- **Treat technical documentation as design contracts**: documentation records the agreed design and implementation is measured against it, not the reverse; mark each gap beside the claim it qualifies with the work that remains.

## Decisions

- **Maintain one Diátaxis mode per document**: a tutorial teaches through one tested path; a how-to solves one task with preconditions, commands, and outcomes; reference states austere facts; an explanation gives rationale and trade-offs, never steps.
- **Lead documentation pages with direct answers**: open with the answer to the reader's primary question, introducing concepts and actors before project shorthand; name real prerequisites at the top and subsequent reading at the end.
- **Link contracts instead of paraphrasing**: state a contract once and link it everywhere else; duplicated explanations drift from the original.
- **Require text-based diagrams for structural relationships**: use diagrams for topology, flow, and ownership when answering reader questions, sourced from plain-text definitions like Mermaid.
- **Keep markdown text unwrapped**: write markdown paragraphs, list items, and rule bullets as continuous unwrapped lines; never hard-wrap lines at 80 characters or any fixed column limit.
- **Scale design documents to decision reversibility**: size a spec to the decision's reach and reversibility; an irreversible choice earns a document, an easily undone one a paragraph.
- **Establish problem constraints before proposing designs**: open with the problem, its constraints, and what success looks like; a proposal whose problem is unstated can only be admired, not judged.
- **Declare explicit goals and non-goals in design documents**: state what the work will do and what it deliberately will not; unstated scope reopens in every review.
- **Document evaluated alternatives and disqualifying trade-offs**: present the options genuinely considered with the trade-offs that eliminated them.
- **Define failure modes rollout steps and rollback strategies**: name failure modes, the migration path, and how the outcome is verified or rolled back; a spec with only a happy path reviews half the work.
- **Supersede historical design documents with living docs**: a design document argues a decision, then stands as its record; fold the outcome into living documentation and supersede the spec.
- **Qualify missing or intended capabilities with footnotes and work markers**: attach a footnote to any claim describing an unbuilt or intended capability; accompany every qualifying footnote with a companion `<!-- TODO(owner): ... -->` comment directly above it so unbuilt gaps remain visible and trackable.
- **Use precise product-neutral architectural terminology**: avoid branded project proper nouns and vague catch-alls like 'the platform'; name concrete boundaries and layers such as the fleet, the control plane (`ctrl`), the worker cells (`cell`), the monorepo, or the cloud foundations.
- **Link third-party tools to official repositories on first mention**: link each third-party tool or open-source component to its official GitHub repository on its first mention in a document (such as `[Kyverno](https://github.com/kyverno/kyverno)`).
- **Lead every authored file with a deterministic header summary**: code and configuration files must open with a 1–2 sentence header (max 220 characters) within the first 5 lines communicating the file's intent, role, and what can be found inside, terminated by the first blank line or closing docstring; keep this summary synchronized whenever altering the file's purpose or scope; agents can read the first 5 lines (`head -n 5 <file>`) to discover file intent without full-file reads when doing large-scale scans.
