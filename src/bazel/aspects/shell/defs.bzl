"""Declare the Shell lint aspect running ShellCheck and shfmt over repository shell scripts."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

shell_aspect = multi_lint_aspect(
    name = "shell",
    rule_kinds = ["sh_library", "sh_binary", "sh_test"],
    extensions = ["sh"],
    steps = [
        {
            "config": ["//src/bazel/aspects:shell/.shellcheckrc"],
            "name": "shellcheck",
            "tool": "//src/bazel/tools:shellcheck",
            "tool_args": ["--severity=style", "--rcfile=src/bazel/aspects/shell/.shellcheckrc"],
        },
        {
            "name": "shfmt",
            "tool": "//src/bazel/tools:shfmt",
            "tool_args": ["-i", "2", "-ci", "-bn", "-s", "-d"],
        },
    ],
)
