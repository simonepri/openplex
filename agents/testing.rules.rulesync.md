---
globs:
  - "**/*_test.py"
  - "**/*_test.go"
  - "**/*.test.ts"
  - "**/*.test.tsx"
  - "**/*.spec.ts"
  - "**/*.spec.tsx"
  - "**/*.tftest.hcl"
---

# Testing

## Principles

- **Verify changes with incremental task runners**: run task runner commands (`mise run test`, `mise run check`, `mise run fix`) freely during development; task runner targets default to affected change sets so iteration stays fast without needing manual target filtering.
- **Justify test creation by defended behavior**: each test adds maintenance cost; write one when it protects behavior worth defending, such as critical paths and edge cases; never write to an arbitrary percentage target.
- **Test observable behavior not implementation**: verify what the code does, not how; implementation refactorings must not break tests.
- **Restrict test suites to first-party code**: tests cover first-party code and chainsaw behavior, nothing else.

## Decisions

- **Derive test expectations independently of implementation**: expected values derive from requirements, invariants, or round-trip properties, never copied from the implementation under test.
- **Name tests after the defect caught**: a test names the failure it catches; if no defect could turn it red, delete it.
- **Test against real dependencies over mocks**: exercise the real subject, not a mock or replica; fake dependencies only when they cannot run in the test environment.
- **Test integration logic not upstream libraries**: a test that still passes with your code and configuration removed tests the dependency, which is upstream's job; test what you built around it.
- **Test rejection paths explicitly**: when testing code that accepts or rejects, assert a rejection too; acceptance alone cannot distinguish a working check from an absent one.
- **Test asymmetric interactions across distinct variants**: when a capability operates across variants, test an interaction between distinct variants, not just within the same variant.
- **Assert contractual outputs only**: assert on outputs the producer commits to, such as identifiers and structured fields, never incidental prose like log lines or message wording.
- **Use table-driven tests for parameterized cases**: use a table or parameterized structure for multiple similar test cases.
- **Keep assertions inside test function bodies**: helpers do setup and cleanup; assertions belong in the test function, never hidden inside helper abstractions.
- **Colocate test files beside tested source**: where the language allows, tests live beside the file they exercise, named after it with a test marker before the extension (`foo_test.py`, `foo.test.ts`, `foo.tftest.hcl`).
