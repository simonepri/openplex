"""Declare the Go lint aspect running gocyclo over repository Go packages."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

go_aspect = multi_lint_aspect(
    name = "go",
    rule_kinds = ["go_library", "go_binary", "go_test"],
    extensions = ["go"],
    steps = [
        {
            "name": "gocyclo",
            "tool": "//src/bazel/tools:gocyclo",
            "tool_args": ["-over", "30", "-ignore", "_test\\.go"],
        },
    ],
)
