"""Tests RuleSync rule indexing, glob pattern matching against changed paths, and evaluation rubric synthesis."""

import unittest

from src.infra.tools.review.context import rules_index


class TestRulesIndex(unittest.TestCase):
    def test_parse_frontmatter(self) -> None:
        content = '---\nglobs:\n  - "src/**/*.py"\n  - "src/**/*.ts"\n---\n# Title\nBody'
        fm, body = rules_index.parse_frontmatter(content)
        self.assertEqual(fm["globs"], ["src/**/*.py", "src/**/*.ts"])
        self.assertIn("# Title", body)

    def test_extract_report_contract_with_table(self) -> None:
        snippet = (
            "Require storage benchmark matrix on storage driver changes\n"
            "| Metric | Baseline | Candidate |\n"
            "| --- | --- | --- |\n"
            "Stored in .review/reports/storage-benchmark.json\n"
        )
        contract = rules_index.extract_report_contract("Storage Benchmark", snippet)
        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual(contract.artifact_pattern, ".review/reports/storage-benchmark.json")
        self.assertIn("Metric", contract.columns)

    def test_matches_rule_glob(self) -> None:
        rule = rules_index.RuleItem(
            rule_id="test_rule",
            title="Rule 1",
            description="Desc",
            domain="coding",
            path="agents/coding.rules.rulesync.md",
            line_number=1,
            is_global=True,
            globs=["src/**/*.py"],
            report_contract=None,
        )
        self.assertTrue(rules_index.matches_rule("src/foo/bar.py", rule))
        self.assertFalse(rules_index.matches_rule("src/foo/bar.ts", rule))

    def test_matches_root_file_with_recursive_glob(self) -> None:
        rule = rules_index.RuleItem(
            rule_id="config_rule",
            title="Config Rule",
            description="Desc",
            domain="configuration",
            path="agents/configuration.rules.rulesync.md",
            line_number=1,
            is_global=True,
            globs=["**/*.toml", "**/*.json"],
            report_contract=None,
        )
        self.assertTrue(rules_index.matches_rule("mise.toml", rule))
        self.assertTrue(rules_index.matches_rule("config/mise.toml", rule))
        self.assertTrue(rules_index.matches_rule("package.json", rule))
        self.assertFalse(rules_index.matches_rule("main.py", rule))


if __name__ == "__main__":
    unittest.main()
