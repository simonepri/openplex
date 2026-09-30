"""Verify Chainsaw test taxonomy labels enforce capability, mutation, cost, and cadence metadata."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import yaml

VALID_CAPABILITIES = frozenset({
    "access",
    "backup",
    "compute",
    "costs",
    "delivery",
    "gitops",
    "identity",
    "networking",
    "observability",
    "resilience",
    "scheduling",
    "secrets",
    "security",
    "storage",
    "team-isolation",
    "workspaces",
})

VALID_CADENCES = frozenset({"commit", "daily", "weekly"})
VALID_MUTATES = frozenset({"none", "scratch", "shared"})
REQUIRED_LABELS = frozenset({"cadence", "capability", "mutates"})


class ChainsawTaxonomyTest(unittest.TestCase):
    def test_all_chainsaw_tests_conform_to_taxonomy(self) -> None:
        runfiles_dir = os.environ.get("TEST_SRCDIR", "")
        workspace_dir = os.environ.get("TEST_WORKSPACE", "_main")
        base = Path(runfiles_dir) / workspace_dir if runfiles_dir else Path.cwd()
        suite_dir = base / "src/infra/definitions/conformance"
        suite_files = sorted(suite_dir.glob("*.test.k8s.yaml"))
        if not suite_files:
            suite_files = sorted(Path("src/infra/definitions/conformance").glob("*.test.k8s.yaml"))

        assert suite_files, "No chainsaw suite files found"

        total_tests = 0
        for suite_path in suite_files:
            content = suite_path.read_text(encoding="utf-8")
            documents = list(yaml.safe_load_all(content))
            test_docs = [doc for doc in documents if doc and doc.get("kind") == "Test"]
            assert len(test_docs) >= 1, f"No Test found in {suite_path}"

            for test in test_docs:
                total_tests += 1
                name = test.get("metadata", {}).get("name")
                assert name, f"Missing name in {suite_path}"
                labels = test.get("metadata", {}).get("labels", {})
                assert isinstance(labels, dict), f"Labels must be a dict in {name}"

                with self.subTest(test=name):
                    missing = REQUIRED_LABELS - set(labels.keys())
                    assert not missing, f"Test {name} missing required labels: {missing}"

                    cadence = labels.get("cadence")
                    assert cadence in VALID_CADENCES, f"Test {name} has invalid cadence '{cadence}'"

                    mutates = labels.get("mutates")
                    assert mutates in VALID_MUTATES, f"Test {name} has invalid mutates '{mutates}'"

                    capability = labels.get("capability")
                    assert capability in VALID_CAPABILITIES, (
                        f"Test {name} has invalid capability '{capability}'"
                    )

        assert total_tests > 0, "Expected at least one test in chainsaw suites"


if __name__ == "__main__":
    unittest.main()
