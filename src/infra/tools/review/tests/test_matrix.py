"""Tests review matrix scorecard parsing, rule criteria matching, and test result evaluation calculations."""

import json
import tempfile
import unittest
from pathlib import Path

from src.infra.tools.review.context.rules_index import ReportContract
from src.infra.tools.review.engine.matrix import evaluate_report_contracts


class TestMatrix(unittest.TestCase):
    def test_evaluate_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = ReportContract(
                title="Performance",
                artifact_pattern=".review/reports/perf.json",
                columns=["Metric", "Value"],
                raw_snippet="",
            )
            res = evaluate_report_contracts(root, [contract])
            self.assertEqual(len(res), 1)
            self.assertTrue(res[0].is_missing)
            self.assertIn("not found", res[0].notes or "")

    def test_evaluate_present_json_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_file = root / ".review/reports/perf.json"
            report_file.parent.mkdir(parents=True)
            report_file.write_text(
                json.dumps([{"metric": "p99", "value": "12ms"}]), encoding="utf-8"
            )

            contract = ReportContract(
                title="Performance",
                artifact_pattern=".review/reports/perf.json",
                columns=["metric", "value"],
                raw_snippet="",
            )
            res = evaluate_report_contracts(root, [contract])
            self.assertEqual(len(res), 1)
            self.assertFalse(res[0].is_missing)
            self.assertIn("p99", res[0].markdown_table)


if __name__ == "__main__":
    unittest.main()
