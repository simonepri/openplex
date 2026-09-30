"""Unit tests defending selective gate and fix generator execution based on workspace diffs.

Verifies pattern matching, core file invalidations, full repository flags, and path filtering.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from src.bazel.tools.diff.gate_diff import (
        CORE_FILES,
        GATE_TRIGGERS,
        GENERATOR_TRIGGERS,
        filter_gates,
        filter_generators,
        is_core_file,
        matches_pattern,
        normalize_gate_label,
    )
except ImportError:
    from gate_diff import (
        CORE_FILES,
        GATE_TRIGGERS,
        GENERATOR_TRIGGERS,
        filter_gates,
        filter_generators,
        is_core_file,
        matches_pattern,
        normalize_gate_label,
    )

ALL_GATES = list(GATE_TRIGGERS.keys())


class GateDiffFilteringTest(unittest.TestCase):
    """Defends filtering of static analysis gates against file diffs and edge cases."""

    def test_markdown_file_does_not_trigger_kubescape_or_trivy(self) -> None:
        gates = [
            "//src/bazel/checks:kubescape",
            "//src/bazel/checks:trivy_source",
            "//src/bazel/checks:function_length",
        ]
        triggered = filter_gates(gates, ["docs/guides/architecture.md"])
        self.assertEqual(triggered, [])

    def test_k8s_manifest_triggers_kubescape_and_cluster_checks(self) -> None:
        gates = [
            "//src/bazel/checks:kubescape",
            "//src/bazel/checks/cluster_network",
            "//src/bazel/checks/workload_portability:workload_portability",
            "//src/bazel/checks/network_exposure:network_exposure",
            "//src/bazel/checks/argocd_links",
            "//src/bazel/checks:opengrep",
            "//src/bazel/checks:trivy_source",
            "//src/bazel/checks/function_length",
        ]
        triggered = filter_gates(gates, ["src/infra/definitions/cluster/app.k8s.yaml"])
        self.assertIn("//src/bazel/checks:kubescape", triggered)
        self.assertIn("//src/bazel/checks/cluster_network", triggered)
        self.assertIn("//src/bazel/checks/workload_portability:workload_portability", triggered)
        self.assertIn("//src/bazel/checks/network_exposure:network_exposure", triggered)
        self.assertIn("//src/bazel/checks/argocd_links", triggered)
        self.assertIn("//src/bazel/checks:opengrep", triggered)
        self.assertNotIn("//src/bazel/checks:trivy_source", triggered)
        self.assertNotIn("//src/bazel/checks/function_length", triggered)

    def test_helm_template_file_triggers_kubescape(self) -> None:
        gates = ["//src/bazel/checks:kubescape", "//src/bazel/checks:function_length"]
        triggered = filter_gates(
            gates,
            ["src/infra/argocd/components/tailscale_access/helm/templates/cloud.yaml"],
        )
        self.assertEqual(triggered, ["//src/bazel/checks:kubescape"])

    def test_package_json_triggers_dependency_and_schema_gates(self) -> None:
        gates = [
            "//src/bazel/checks:dev_tool_isolation",
            "//src/bazel/checks:json_schemas",
            "//src/bazel/checks:license_images",
            "//src/bazel/checks:license_source",
            "//src/bazel/checks:trivy_source",
            "//src/bazel/checks:opengrep",
            "//src/bazel/checks:kubescape",
        ]
        triggered = filter_gates(gates, ["package.json"])
        self.assertIn("//src/bazel/checks:dev_tool_isolation", triggered)
        self.assertIn("//src/bazel/checks:json_schemas", triggered)
        self.assertIn("//src/bazel/checks:license_images", triggered)
        self.assertIn("//src/bazel/checks:license_source", triggered)
        self.assertIn("//src/bazel/checks:trivy_source", triggered)
        self.assertIn("//src/bazel/checks:opengrep", triggered)
        self.assertNotIn("//src/bazel/checks:kubescape", triggered)

    def test_pyproject_toml_triggers_dev_tool_and_license_gates(self) -> None:
        gates = [
            "//src/bazel/checks:dev_tool_isolation",
            "//src/bazel/checks:license_source",
            "//src/bazel/checks:trivy_source",
            "//src/bazel/checks/function_length",
        ]
        triggered = filter_gates(gates, ["pyproject.toml"])
        self.assertIn("//src/bazel/checks:dev_tool_isolation", triggered)
        self.assertIn("//src/bazel/checks:license_source", triggered)
        self.assertIn("//src/bazel/checks:trivy_source", triggered)
        self.assertNotIn("//src/bazel/checks/function_length", triggered)

    def test_python_source_triggers_function_length_and_opengrep(self) -> None:
        gates = [
            "//src/bazel/checks/function_length",
            "//src/bazel/checks:opengrep",
            "//src/bazel/checks:gazelle_drift",
            "//src/bazel/checks:kubescape",
        ]
        triggered = filter_gates(gates, ["src/bazel/tools/diff/gate_diff.py"])
        self.assertIn("//src/bazel/checks/function_length", triggered)
        self.assertIn("//src/bazel/checks:opengrep", triggered)
        self.assertIn("//src/bazel/checks:gazelle_drift", triggered)
        self.assertNotIn("//src/bazel/checks:kubescape", triggered)

    def test_dotenv_file_triggers_dotenv_gate(self) -> None:
        gates = ["//src/bazel/checks:dotenv", "//src/bazel/checks:kubescape"]
        self.assertEqual(filter_gates(gates, [".env"]), ["//src/bazel/checks:dotenv"])
        self.assertEqual(filter_gates(gates, [".env.local"]), ["//src/bazel/checks:dotenv"])
        self.assertEqual(filter_gates(gates, ["src/app/custom.env"]), ["//src/bazel/checks:dotenv"])

    def test_json_schema_file_triggers_json_schemas_gate(self) -> None:
        gates = ["//src/bazel/checks:json_schemas", "//src/bazel/checks/function_length"]
        triggered = filter_gates(gates, ["config.schema.json"])
        self.assertEqual(triggered, ["//src/bazel/checks:json_schemas"])

    def test_rulesync_markdown_triggers_rulesync_gate(self) -> None:
        gates = ["//src/bazel/checks/rulesync", "//src/bazel/checks:kubescape"]
        triggered = filter_gates(gates, ["agents/coding.rules.rulesync.md"])
        self.assertEqual(triggered, ["//src/bazel/checks/rulesync"])

    def test_codeowners_file_triggers_codeowners_gate(self) -> None:
        gates = ["//src/bazel/checks/records:codeowners", "//src/bazel/checks:function_length"]
        self.assertEqual(
            filter_gates(gates, ["CODEOWNERS"]),
            ["//src/bazel/checks/records:codeowners"],
        )
        self.assertEqual(
            filter_gates(gates, [".github/CODEOWNERS"]),
            ["//src/bazel/checks/records:codeowners"],
        )

    def test_team_definitions_trigger_team_records_and_codeowners(self) -> None:
        gates = [
            "//src/bazel/checks/records:codeowners",
            "//src/bazel/checks/records:team_records",
            "//src/bazel/checks:kubescape",
        ]
        triggered = filter_gates(gates, ["src/infra/definitions/teams/alpha.yaml"])
        self.assertIn("//src/bazel/checks/records:codeowners", triggered)
        self.assertIn("//src/bazel/checks/records:team_records", triggered)
        self.assertNotIn("//src/bazel/checks:kubescape", triggered)

    def test_workspace_definitions_trigger_coder_templates(self) -> None:
        gates = ["//src/bazel/checks:coder_templates", "//src/bazel/checks:kubescape"]
        triggered = filter_gates(gates, ["src/infra/definitions/workspaces/dev/main.tf"])
        self.assertEqual(triggered, ["//src/bazel/checks:coder_templates"])

    def test_lock_files_trigger_lock_drift_gate(self) -> None:
        gates = ["//src/bazel/checks:lock_drift", "//src/bazel/checks:kubescape"]
        for lock_file in [
            "package.json",
            "pnpm-lock.yaml",
            "pyproject.toml",
            "requirements_lock.txt",
            "requirements_dev_lock.txt",
            "uv.lock",
        ]:
            with self.subTest(lock_file=lock_file):
                triggered = filter_gates(gates, [lock_file])
                self.assertIn("//src/bazel/checks:lock_drift", triggered)
        self.assertEqual(filter_gates(gates, ["docs/readme.md"]), [])

    def test_source_files_trigger_dups_gate(self) -> None:
        gates = ["//src/bazel/checks:dups", "//src/bazel/checks:kubescape"]
        for source_file in [
            "src/app/main.py",
            "src/pkg/service.go",
            "src/web/index.ts",
            "src/infra/main.tf",
            "src/data/config.yaml",
        ]:
            with self.subTest(source_file=source_file):
                triggered = filter_gates(gates, [source_file])
                self.assertIn("//src/bazel/checks:dups", triggered)
        self.assertEqual(filter_gates(gates, ["docs/readme.md"]), [])

    def test_go_source_triggers_cyclo_gate(self) -> None:
        gates = ["//src/bazel/checks:cyclo", "//src/bazel/checks:kubescape"]
        triggered = filter_gates(gates, ["src/pkg/service.go"])
        self.assertIn("//src/bazel/checks:cyclo", triggered)
        self.assertEqual(filter_gates(gates, ["src/app/main.py"]), [])
        self.assertEqual(filter_gates(gates, ["package.json"]), [])

    def test_core_file_change_triggers_all_gates(self) -> None:
        for core_file in CORE_FILES:
            with self.subTest(core_file=core_file):
                triggered = filter_gates(ALL_GATES, [core_file])
                self.assertEqual(triggered, ALL_GATES)

    def test_run_all_flag_triggers_all_gates(self) -> None:
        triggered = filter_gates(ALL_GATES, [], run_all=True)
        self.assertEqual(triggered, ALL_GATES)

    def test_clean_state_triggers_no_gates(self) -> None:
        triggered = filter_gates(ALL_GATES, [], run_all=False)
        self.assertEqual(triggered, [])

    def test_file_containing_ifchange_triggers_ifttt_gate(self) -> None:
        gates = ["//src/bazel/checks:ifttt", "//src/bazel/checks:kubescape"]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            match_file = tmp_path / "with_ifchange.txt"
            match_file.write_text("# LINT.IfChange\nvalue = 1\n", encoding="utf-8")

            nomatch_file = tmp_path / "without_ifchange.txt"
            nomatch_file.write_text("plain text without tag\n", encoding="utf-8")

            triggered_match = filter_gates(
                gates,
                ["with_ifchange.txt"],
                repo_root=tmp_path,
            )
            self.assertEqual(triggered_match, ["//src/bazel/checks:ifttt"])

            triggered_nomatch = filter_gates(
                gates,
                ["without_ifchange.txt"],
                repo_root=tmp_path,
            )
            self.assertEqual(triggered_nomatch, [])


class GeneratorDiffFilteringTest(unittest.TestCase):
    """Defends filtering of fix generators against workspace diffs and edge cases."""

    def test_package_json_triggers_pnpm_generator_only(self) -> None:
        generators = filter_generators(["package.json"])
        self.assertIn("pnpm", generators)
        self.assertNotIn("uv", generators)

    def test_pyproject_toml_triggers_uv_generator_only(self) -> None:
        generators = filter_generators(["pyproject.toml"])
        self.assertIn("uv", generators)
        self.assertNotIn("pnpm", generators)

    def test_team_definitions_trigger_team_records_generator(self) -> None:
        generators = filter_generators(["src/infra/definitions/teams/beta.yaml"])
        self.assertEqual(generators, ["team_records"])

    def test_project_yaml_triggers_team_records_generator(self) -> None:
        generators = filter_generators(["deployment/project.yaml"])
        self.assertEqual(generators, ["team_records"])

    def test_rulesync_markdown_triggers_rulesync_generator(self) -> None:
        generators = filter_generators(["agents/documentation.rules.rulesync.md"])
        self.assertEqual(generators, ["rulesync"])

    def test_build_file_triggers_gazelle_generator(self) -> None:
        generators = filter_generators(["src/infra/BUILD.bazel"])
        self.assertEqual(generators, ["gazelle"])

    def test_artwork_file_triggers_artwork_generator(self) -> None:
        generators = filter_generators(["src/infra/docs/artwork/wordmark.svg"])
        self.assertEqual(generators, ["artwork"])

    def test_core_file_change_triggers_all_generators(self) -> None:
        generators = filter_generators(["MODULE.bazel"])
        self.assertEqual(generators, list(GENERATOR_TRIGGERS.keys()))

    def test_run_all_flag_triggers_all_generators(self) -> None:
        generators = filter_generators([], run_all=True)
        self.assertEqual(generators, list(GENERATOR_TRIGGERS.keys()))

    def test_clean_state_triggers_no_generators(self) -> None:
        generators = filter_generators([], run_all=False)
        self.assertEqual(generators, [])


class HelperFunctionsTest(unittest.TestCase):
    """Defends core file identification and pattern matching primitives."""

    def test_is_core_file_identifies_root_configuration_only(self) -> None:
        self.assertTrue(is_core_file("BUILD.bazel"))
        self.assertTrue(is_core_file("MODULE.bazel"))
        self.assertFalse(is_core_file("src/foo/BUILD.bazel"))
        self.assertFalse(is_core_file("docs/readme.md"))

    def test_matches_pattern_prefix_and_wildcards(self) -> None:
        self.assertTrue(matches_pattern("*.py", "src/foo/bar.py"))
        self.assertTrue(matches_pattern("Dockerfile*", "Dockerfile.base"))
        self.assertTrue(matches_pattern("Dockerfile*", "src/images/Dockerfile"))
        self.assertTrue(matches_pattern("src/examples/**", "src/examples/cron/app.py"))
        self.assertFalse(matches_pattern("src/examples/**", "src/infra/cron/app.py"))

    def test_normalize_gate_label_handles_bzlmod_and_check_suffixes(self) -> None:
        self.assertEqual(
            normalize_gate_label("@@//src/bazel/checks:kubescape_check"),
            "//src/bazel/checks:kubescape",
        )
        self.assertEqual(
            normalize_gate_label("@@//src/bazel/checks/cluster_network:cluster_network"),
            "//src/bazel/checks/cluster_network",
        )
        self.assertEqual(
            normalize_gate_label("//src/bazel/checks:lock_drift_check"),
            "//src/bazel/checks:lock_drift",
        )
        self.assertEqual(
            normalize_gate_label("@@//src/bazel/checks/source_names:source_names"),
            "//src/bazel/checks/source_names:source_names",
        )


if __name__ == "__main__":
    unittest.main()
