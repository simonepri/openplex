"""Declare the YAML lint aspect running yamllint and yamlfmt across manifests and configuration files."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

yaml_aspect = multi_lint_aspect(
    name = "yaml",
    rule_tags = ["data", "chainsaw-suite", "chainsaw-config"],
    extensions = ["yaml", "yml"],
    steps = [
        {
            "config": ["//src/bazel/aspects:yaml/yamllint.yaml"],
            "name": "yamllint",
            "tag": "data",
            "tool": "//src/bazel/tools:yamllint",
            "tool_args": ["--strict", "--config-file", "src/bazel/aspects/yaml/yamllint.yaml"],
        },
        {
            "name": "chainsaw-suite",
            "per_file": True,
            "tag": "chainsaw-suite",
            "tool": "//src/bazel/tools:chainsaw",
            "tool_args": ["lint", "test", "--file"],
        },
        {
            "name": "chainsaw-config",
            "per_file": True,
            "tag": "chainsaw-config",
            "tool": "//src/bazel/tools:chainsaw",
            "tool_args": ["lint", "configuration", "--file"],
        },
    ],
)
