"""Test header docstring retention and generation logic for BUILD.bazel files updated by Gazelle."""

import unittest

from src.bazel.rules.lint_aspect.build_headers import normalize


class BuildHeadersTest(unittest.TestCase):
    @staticmethod
    def test_new_package_gets_a_purpose_without_losing_directives() -> None:
        content = '# gazelle:python_generation_mode file\n\nload("defs.bzl", "rule")\n'
        updated = normalize(content, "src/infra/tools/cloud_emulator")
        assert updated.startswith('"""Build targets for src/infra/tools/cloud_emulator."""\n\n')
        assert updated.endswith(content)
        assert updated == normalize(updated, "src/infra/tools/cloud_emulator")

    @staticmethod
    def test_new_load_cannot_displace_authored_purpose() -> None:
        purpose = (
            '"""Check targets for the Argo CD applications.\n\nAuthored detail stays intact.\n"""\n'
        )
        load = 'load("@rules_python//python:defs.bzl", "py_test")\n\n'
        content = load + purpose + '\npy_test(name = "profiles_test")\n'
        updated = normalize(content, "src/infra/argocd/apps")
        assert updated.startswith(purpose + "\n")
        assert updated.count(purpose) == 1
        assert load in updated
        assert 'py_test(name = "profiles_test")' in updated
        assert updated == normalize(updated, "src/infra/argocd/apps")

    @staticmethod
    def test_correct_header_is_unchanged() -> None:
        content = '"""A deliberate purpose."""\n\nload("defs.bzl", "rule")\n'
        assert normalize(content, "src/package") == content


if __name__ == "__main__":
    unittest.main()
