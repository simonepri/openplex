#!/usr/bin/env python3
"""Test that root readme.md matches the generated output from src/infra/docs/readme.md."""

from __future__ import annotations

import unittest

try:
    from readme import main, rewrite_links
except ImportError:
    from src.infra.docs.readme import main, rewrite_links


class ReadmeTest(unittest.TestCase):
    def test_relative_links_and_images_are_rewritten(self) -> None:
        source = (
            '<a href="./architecture.md"><img src="./assets/logo.svg" /></a>\n'
            "- [Developer Guide](./developer.md)\n"
        )
        expected = (
            '<a href="src/infra/docs/architecture.md"><img src="src/infra/docs/assets/logo.svg" /></a>\n'
            "- [Developer Guide](src/infra/docs/developer.md)\n"
        )
        self.assertEqual(rewrite_links(source), expected)

    def test_external_and_anchor_links_are_preserved(self) -> None:
        source = '<a href="#local">Local</a> · [mise](https://github.com/jdx/mise)'
        self.assertEqual(rewrite_links(source), source)

    def test_committed_readme_matches_generator(self) -> None:
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
