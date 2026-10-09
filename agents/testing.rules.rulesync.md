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

- **Verify changes with incremental task runners**: run task runner commands (`mise run test`, `mise run fix`, `mise run build`) freely during development; task runner targets default to affected change sets so iteration stays fast without needing manual target filtering.
- **Justify test creation by defended behavior**: each test adds maintenance cost; write one when it protects behavior worth defending, such as critical paths and edge cases; never write to an arbitrary percentage target.
- **Prohibit shadow runtime simulation in test suites**: test suites must not re-implement or emulate the execution engine, scheduler, or template renderer of external platforms; if the platform runtime cannot execute in the test environment, assert on static manifests or validated schemas rather than simulating runtime behavior in code.
- **Test observable behavior not implementation**: verify what the code does, not how; implementation refactorings must not break tests.
- **Restrict test suites to first-party code**: tests cover first-party code and chainsaw behavior, nothing else.

## Decisions

- **Justify script and binary unit tests by internal logic**: pure command forwarders, argument passthroughs, declarative wrappers, and linear glue scripts do not warrant unit tests; author unit tests for scripts or binaries only when they implement internal algorithmic state, distinct error recovery paths, or complex input parsing worth defending.
- **Derive test expectations independently of implementation**: expected values derive from requirements, invariants, or round-trip properties, never copied from the implementation under test.
- **Name tests after the defect caught**: a test names the failure it catches; if no defect could turn it red, delete it.
- **Test against real dependencies over mocks**: exercise the real subject, not a mock or replica; when external systems (such as daemons, containers, or remote APIs) cannot execute within a unit test sandbox, verify behavior through integration suites or declarative schema assertions rather than constructing synthetic mock hierarchies.
- **Prohibit implementation verification via mock introspection**: assertions in tests must verify return values, state changes, or emitted artifacts; never assert on private function invocation counts, internal collaborator argument forwarding, or call sequences through mock object introspection.
- **Prohibit imperative dictionary traversal for Kubernetes manifests**: manifest validation must use declarative schema validators or policy engines; do not author imperative unit test suites that deserialize rendered YAML into nested dictionaries to assert presence of fields or kinds.
- **Require declarative testing frameworks for cluster conformance**: end-to-end cluster conformance, acceptance, and admission validation must be authored as declarative suites in dedicated testing frameworks; do not write imperative Python or shell CLI wrapper scripts that poll or parse command-line outputs.
- **Prohibit unit testing of shell scripts via synthetic environments**: shell scripts are integration glue; do not write unit tests that fake execution environments by overriding system search paths (`$PATH`), injecting mock binaries, or programmatically editing script source code; verify shell scripts through end-to-end execution of the capability they coordinate, or rewrite the logic in a typed language if fine-grained unit testing is required.
- **Test integration logic not upstream libraries**: a test that still passes with your code and configuration removed tests the dependency, which is upstream's job; test what you built around it.
- **Test rejection paths explicitly**: when testing code that accepts or rejects, assert a rejection too; acceptance alone cannot distinguish a working check from an absent one.
- **Test asymmetric interactions across distinct variants**: when a capability operates across variants, test an interaction between distinct variants, not just within the same variant.
- **Assert contractual outputs only**: assert on outputs the producer commits to, such as identifiers and structured fields, never incidental prose like log lines or message wording.
- **Use table-driven tests for parameterized cases**: use a table or parameterized structure for multiple similar test cases.
- **Keep assertions inside test function bodies**: helpers do setup and cleanup; assertions belong in the test function, never hidden inside helper abstractions.
- **Colocate test files beside tested source**: where the language allows, tests live beside the file they exercise, named after it with a test marker before the extension (`foo_test.py`, `foo.test.ts`, `foo.tftest.hcl`).
