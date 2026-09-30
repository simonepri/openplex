"""Tests review report formatting, markdown serialization, and JSON schema compliance for review results."""

from __future__ import annotations

import unittest

from src.infra.tools.review.engine.report import (
    compute_size_bucket,
    effective_verdict,
    parse_review,
    render_finding,
    render_report,
)


class TestReport(unittest.TestCase):
    def test_compute_size_bucket(self) -> None:
        self.assertEqual(compute_size_bucket(files=1, sloc=10), "XS")
        self.assertEqual(compute_size_bucket(files=5, sloc=150), "S")
        self.assertEqual(compute_size_bucket(files=8, sloc=300), "M")
        self.assertEqual(compute_size_bucket(files=15, sloc=800), "L")
        self.assertEqual(compute_size_bucket(files=30, sloc=1500), "XL")
        self.assertEqual(compute_size_bucket(files=60, sloc=5000), "XXL")

    def test_render_finding_small_path(self) -> None:
        finding = {
            "severity": "blocker",
            "location": "src/infra/tools/review/cli.py:42",
            "body": "Fix unhandled exception on missing ref.",
        }
        with_small = render_finding(finding, small_path=True)
        self.assertEqual(
            with_small,
            "- <small>`src/infra/tools/review/cli.py:42`</small> — Fix unhandled exception on missing ref.",
        )

        without_small = render_finding(finding, small_path=False)
        self.assertEqual(
            without_small,
            "- `src/infra/tools/review/cli.py:42` — Fix unhandled exception on missing ref.",
        )

    def test_effective_verdict(self) -> None:
        self.assertEqual(
            effective_verdict({
                "verdict": "LGTM",
                "findings": [{"severity": "blocker", "location": "a.py:1", "body": "err"}],
            }),
            "DO NOT MERGE",
        )
        self.assertEqual(
            effective_verdict({
                "verdict": "LGTM",
                "findings": [{"severity": "improvement", "location": "a.py:1", "body": "opt"}],
            }),
            "CHANGES REQUESTED",
        )
        self.assertEqual(
            effective_verdict({
                "verdict": "LGTM",
                "findings": [{"severity": "nit", "location": "a.py:1", "body": "typo"}],
            }),
            "LGTM",
        )

    def test_render_report_full(self) -> None:
        payload = {
            "verdict": "CHANGES REQUESTED",
            "size": "M",
            "summary": "Need cleanup in networking layer.",
            "split": ["PR 1: Split networking", "PR 2: Update callers"],
            "findings": [
                {
                    "severity": "improvement",
                    "location": "src/net.py:10",
                    "body": "Use connection pool.",
                },
                {
                    "severity": "nit",
                    "location": "src/net.py:20",
                    "body": "Fix spelling.",
                },
            ],
        }
        parsed = parse_review(payload)
        md = render_report(parsed, small_path=True)
        self.assertIn(
            "🏷️ **Verdict**: `CHANGES REQUESTED [M]` — Need cleanup in networking layer.", md
        )
        self.assertIn(
            "✂️ **Split suggestion**:\n- PR 1: Split networking\n- PR 2: Update callers", md
        )
        self.assertIn(
            "🟡 **Improvement**\n- <small>`src/net.py:10`</small> — Use connection pool.", md
        )
        self.assertIn("🔵 **Nit**\n- <small>`src/net.py:20`</small> — Fix spelling.", md)
        self.assertNotIn("🔴 **Blocker**", md)


if __name__ == "__main__":
    unittest.main()
