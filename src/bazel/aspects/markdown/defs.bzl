"""Declare the Markdown lint aspect running rumdl and mmdlint over documentation and Mermaid diagrams."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

markdown_aspect = multi_lint_aspect(
    name = "markdown",
    rule_tags = ["data"],
    extensions = ["md"],
    steps = [
        {
            "config": ["//src/bazel/aspects:markdown/rumdl.toml"],
            "name": "rumdl",
            "tool": "//src/bazel/tools:rumdl",
            "tool_args": [
                "check",
                "--config",
                "src/bazel/aspects/markdown/rumdl.toml",
                "--fail-on",
                "any",
                "--deny-config-warnings",
            ],
        },
        {
            "name": "mmdlint",
            "tool": "//src/bazel/tools:mmdlint",
            "tool_args": [],
        },
    ],
)
