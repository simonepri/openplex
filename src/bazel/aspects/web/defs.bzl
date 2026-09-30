"""Declare the Web lint aspect running oxlint and oxfmt over TypeScript and JavaScript web assets."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

web_aspect = multi_lint_aspect(
    name = "web",
    rule_tags = ["web-source"],
    extensions = ["ts", "tsx", "js", "jsx", "mjs"],
    steps = [
        {
            "config": ["//src/bazel/aspects:web/.oxlintrc.json"],
            "name": "oxlint",
            "tool": "//src/bazel/tools:oxlint",
            "tool_args": [
                "-c",
                "src/bazel/aspects/web/.oxlintrc.json",
                "--deny-warnings",
                "--report-unused-disable-directives-severity=error",
            ],
        },
        {
            "name": "oxfmt",
            "tool": "//src/bazel/tools:oxfmt",
            "tool_args": ["--check"],
        },
    ],
)
