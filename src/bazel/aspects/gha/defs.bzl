"""Declare the GitHub Actions lint aspect running actionlint and zizmor over workflow definitions."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

gha_aspect = multi_lint_aspect(
    name = "gha",
    rule_tags = ["github-workflow"],
    extensions = ["yml", "yaml"],
    steps = [
        {
            "name": "actionlint",
            "tool": "//src/bazel/tools:actionlint",
            "tool_args": [],
        },
        {
            "name": "zizmor",
            "tool": "//src/bazel/tools:zizmor",
            "tool_args": [
                "--no-progress",
                "--offline",
                "--persona=pedantic",
                "--min-severity=informational",
                "--min-confidence=low",
                "--strict-collection",
            ],
        },
    ],
)
