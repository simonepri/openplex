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
