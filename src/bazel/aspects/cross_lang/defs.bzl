"""Declare the cross-language lint aspect enforcing repository-wide secret detection and invariant rules."""

load("//src/bazel/rules/lint_aspect:defs.bzl", "multi_lint_aspect")

cross_lang_aspect = multi_lint_aspect(
    name = "cross_lang",
    rule_kinds = [
        "py_binary",
        "py_library",
        "py_test",
        "sh_binary",
        "sh_library",
        "sh_test",
    ],
    rule_tags = [
        "chainsaw-fixture",
        "chainsaw-suite",
        "data",
        "generated",
        "github-workflow",
        "helm-template",
        "helm-values",
        "k8s-manifest",
        "kustomize-patch",
        "kustomize-root",
    ],
    steps = [
        {
            "config": ["//src/bazel/aspects:cross_lang/gitleaks.toml"],
            "name": "gitleaks",
            "tool": "//src/bazel/tools:gitleaks",
            "tool_args": [
                "dir",
                "--config",
                "src/bazel/aspects/cross_lang/gitleaks.toml",
                "--redact",
                "--verbose",
            ],
        },
    ],
)
