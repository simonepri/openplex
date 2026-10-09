"""Re-export lint aspects and workspace formatting runner for backward-compatible consumption."""

load("//src/bazel/rules/lint_aspect:aspect.bzl", _lint_aspect = "lint_aspect", _multi_lint_aspect = "multi_lint_aspect")
load("//src/bazel/rules/lint_aspect:format.bzl", _format_all = "format_all")

lint_aspect = _lint_aspect
multi_lint_aspect = _multi_lint_aspect
format_all = _format_all
