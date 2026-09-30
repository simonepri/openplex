"""Declare the OpenTofu lint aspect running tofu fmt and provider-aware TFLint checks on Terraform sources."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

# tofu fmt and tflint check every terraform file tracked in the repository.
tofu_aspect = multi_lint_aspect(
    name = "tofu",
    extensions = ["tf"],
    rule_tags = ["data"],
    steps = [
        {
            "name": "tofu-fmt",
            "per_file": True,
            "tool": "//src/bazel/tools:tofu",
            "tool_args": ["fmt", "-check", "-no-color"],
        },
        {
            "chdir": True,
            "config": ["//src/bazel/aspects:tofu/tflint.hcl"],
            "name": "tflint",
            "per_file": True,
            "tool": "//src/bazel/tools:tflint",
            "tool_args": [
                "--config",
                "$root/src/bazel/aspects/tofu/tflint.hcl",
                "--call-module-type=none",
                "--minimum-failure-severity=notice",
                "--filter",
            ],
        },
    ],
)

tofu_fmt_aspect = tofu_aspect
