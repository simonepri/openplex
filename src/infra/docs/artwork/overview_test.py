#!/usr/bin/env python3
"""Test that the overview graphic names every stage and workload and that the committed file matches the generator."""

from __future__ import annotations

import unittest

from overview import STAGES, WORKLOADS, main, overview


class OverviewTest(unittest.TestCase):
    def test_every_stage_and_workload_is_drawn(self) -> None:
        graphic = overview()
        for title, _, copy in STAGES:
            self.assertIn(f">{title}<", graphic)
            for line in copy:
                self.assertIn(f">{line}<", graphic)
        for name in WORKLOADS:
            self.assertIn(">" + name.replace("&", "&amp;") + "<", graphic)

    def test_text_is_escaped(self) -> None:
        self.assertNotIn("& serving", overview())

    def test_committed_graphic_matches_generator(self) -> None:
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
