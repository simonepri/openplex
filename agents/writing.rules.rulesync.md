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
