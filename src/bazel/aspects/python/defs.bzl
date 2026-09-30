"""Declare the Python lint aspect running Ruff and ty type checking across Python libraries and binaries."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

python_aspect = multi_lint_aspect(
    name = "python",
    rule_kinds = ["py_library", "py_binary", "py_test"],
    extensions = ["py"],
    steps = [
        {
            "config": ["//src/bazel/aspects:python/ruff.toml"],
            "name": "ruff-check",
            "tool": "//src/bazel/tools:ruff",
            "tool_args": ["check", "--config", "src/bazel/aspects/python/ruff.toml"],
        },
        {
            "config": ["//src/bazel/aspects:python/ruff.toml"],
            "name": "ruff-format",
            "tool": "//src/bazel/tools:ruff",
            "tool_args": ["format", "--check", "--config", "src/bazel/aspects/python/ruff.toml"],
        },
        {
            "config": ["//src/bazel/aspects:python/ty.toml"],
            "name": "ty",
            "needs_deps": True,
            "tool": "//src/bazel/tools:ty",
            "tool_args": ["check", "--config-file", "src/bazel/aspects/python/ty.toml"],
        },
    ],
)
