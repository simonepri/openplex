"""Tests GitHub sticky comment rendering, state updates, previous review collapse logic, and merge gate checks."""

from __future__ import annotations

import unittest

from src.infra.tools.review.engine.github_comment import (
    REVIEW_CLOSE,
    REVIEW_OPEN,
    STICKY_MARKER,
    ReviewStats,
    build_finish_comment,
    build_start_comment,
    extract_previous_review,
    format_duration,
    format_tokens,
    parse_override_from_body,
    parse_stats_marker,
)


class TestGithubComment(unittest.TestCase):
    """Defends observable behavior of GitHub comment lifecycle management."""

    def test_parse_override_from_body_valid(self) -> None:
        body = "Some description\n\nNO_LGTM=Critical hotfix for production SEV-1\nMore info"
        active, reason = parse_override_from_body(body)
        self.assertTrue(active)
        self.assertEqual(reason, "Critical hotfix for production SEV-1")

    def test_parse_override_from_body_placeholder_rejected(self) -> None:
        body = "PR template\nNO_LGTM=<reason>\n"
        active, reason = parse_override_from_body(body)
        self.assertFalse(active)
        self.assertEqual(reason, "")

    def test_parse_override_from_body_missing(self) -> None:
        active, reason = parse_override_from_body("Just a regular PR description.")
        self.assertFalse(active)
        self.assertEqual(reason, "")
        active_none, _ = parse_override_from_body(None)
        self.assertFalse(active_none)

    def test_extract_previous_review(self) -> None:
        body = f"Some header\n{REVIEW_OPEN}\n🏷️ **Verdict**: `LGTM [S]` — Good change\n{REVIEW_CLOSE}\nSome footer"
        extracted = extract_previous_review(body)
        self.assertEqual(extracted, "🏷️ **Verdict**: `LGTM [S]` — Good change")

    def test_extract_previous_review_missing_markers(self) -> None:
        self.assertEqual(extract_previous_review("No markers here"), "")

    def test_parse_stats_marker(self) -> None:
        body = "<!-- code-review-sticky -->\n<!-- review-stats runs=3 tokens=45200 cost=0.125000 tokens_available=true cost_available=true -->\n"
        stats = parse_stats_marker(body)
        self.assertEqual(stats.runs, 3)
        self.assertEqual(stats.tokens, 45200)
        self.assertAlmostEqual(stats.cost, 0.125, places=3)
        self.assertTrue(stats.tokens_available)
        self.assertTrue(stats.cost_available)

    def test_format_tokens_and_duration(self) -> None:
        self.assertEqual(format_tokens(500), "500")
        self.assertEqual(format_tokens(1500), "1k")
        self.assertEqual(format_tokens(1250000), "1.2M")
        self.assertEqual(format_duration(45000), "45s")
        self.assertEqual(format_duration(75000), "1m15s")

    def test_build_start_comment_rendering(self) -> None:
        comment = build_start_comment(
            sha="abcdef1234567890",
            stats=ReviewStats(runs=2, tokens=10000, cost=0.05),
            previous_review="🏷️ **Verdict**: `LGTM [S]` — Previous run ok",
            repo="org/repo",
            job_url="https://github.com/org/repo/actions/runs/123",
        )
        self.assertIn(STICKY_MARKER, comment)
        self.assertIn("<em>Reviewing", comment)
        self.assertIn("#abcdef1", comment)
        self.assertIn("2 runs", comment)
        self.assertIn("<details>", comment)
        self.assertIn("📋 Previous review — LGTM [S]", comment)
        self.assertIn("[job]", comment)

    def test_build_finish_comment_lgtm_passes_gate(self) -> None:
        review_data = {
            "verdict": "LGTM",
            "size": "XS",
            "summary": "Clean code changes.",
            "split": [],
            "findings": [],
        }
        comment, gate = build_finish_comment(
            sha="1234567890abcdef",
            outcome="success",
            review_content=review_data,
            prev_stats=ReviewStats(runs=1, tokens=5000, cost=0.02),
            this_tokens=3000,
            this_duration_ms=25000,
            this_cost=0.01,
            repo="org/repo",
        )
        self.assertEqual(gate, "pass")
        self.assertIn("🏷️ **Verdict**: `LGTM [XS]`", comment)
        self.assertNotIn("This review is blocking", comment)
        self.assertIn("2 runs", comment)

    def test_build_finish_comment_findings_blocks_gate(self) -> None:
        review_data = {
            "verdict": "CHANGES REQUESTED",
            "size": "XS",
            "summary": "Missing validation.",
            "split": [],
            "findings": [
                {
                    "severity": "improvement",
                    "location": "src/app.py:42",
                    "body": "Add length constraint.",
                }
            ],
        }
        comment, gate = build_finish_comment(
            sha="1234567890abcdef",
            outcome="success",
            review_content=review_data,
            prev_stats=ReviewStats(),
            repo="org/repo",
        )
        self.assertEqual(gate, "block")
        self.assertIn("🏷️ **Verdict**: `CHANGES REQUESTED [XS]`", comment)
        self.assertIn("This review is blocking", comment)
        self.assertIn("<small>`src/app.py:42`</small>", comment)

    def test_build_finish_comment_override_unblocks(self) -> None:
        review_data = {
            "verdict": "DO NOT MERGE",
            "size": "S",
            "summary": "Severe bug.",
            "split": [],
            "findings": [
                {
                    "severity": "blocker",
                    "location": "src/main.py:1",
                    "body": "Fatal error.",
                }
            ],
        }
        comment, gate = build_finish_comment(
            sha="1234567890abcdef",
            outcome="success",
            review_content=review_data,
            prev_stats=ReviewStats(),
            override_active=True,
            override_reason="Will patch in follow-up PR #99",
            repo="org/repo",
        )
        self.assertEqual(gate, "override")
        self.assertIn("Review gate overridden", comment)
        self.assertIn("Will patch in follow-up PR #99", comment)


if __name__ == "__main__":
    unittest.main()
