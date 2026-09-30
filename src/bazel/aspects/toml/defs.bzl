"""Declare the TOML lint aspect formatting and validating TOML files using Taplo."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "lint_aspect")

toml_aspect = lint_aspect(
    name = "toml",
    tool = "//src/bazel/tools:taplo",
    tool_args = ["fmt", "--check", "--diff"],
    rule_tags = ["data"],
    extensions = ["toml"],
)
