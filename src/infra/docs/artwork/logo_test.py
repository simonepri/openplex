#!/usr/bin/env python3
"""Test tool icon selection, ring ordering, icon placement, and that the committed logo matches its sources."""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from logo import (
    EmblemError,
    emblem,
    main,
    place,
    png_colours,
    ring_position,
    tool_icons,
)

ORANGE = '<svg viewBox="0 0 8 8"><path fill="#ea580c" d="M0 0h8v8H0z"/></svg>'
BLUE = '<svg viewBox="0 0 8 8"><path style="fill:#2563eb" d="M0 0h8v8H0z"/></svg>'
BLACK = '<svg viewBox="0 0 8 8"><path d="M0 0h8v8H0z"/></svg>'
BLUE_WITH_WHITE_DETAIL = (
    '<svg viewBox="0 0 8 8"><path fill="#2563eb" d="M0 0h8v8H0z"/>'
    '<path fill="#fff" d="M1 1h1"/><path fill="#fff" d="M2 2h1"/><path fill="#fff" d="M3 3h1"/>'
    '<path fill="#fff" d="M4 4h1"/><path fill="#fff" d="M5 5h1"/><path fill="#fff" d="M6 6h1"/>'
    '<path fill="#fff" d="M7 7h1"/><path fill="#fff" d="M1 7h1"/></svg>'
)
WORDMARK = '<svg viewBox="0 0 400 100"><path fill="currentColor" d="M0 0h400v100H0z"/></svg>'


def _png(pixels: list[tuple[int, int, int, int]]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    row = b"\x00" + b"".join(bytes(pixel) for pixel in pixels)
    header = struct.pack(">IIBBBBB", len(pixels), 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row))
        + chunk(b"IEND", b"")
    )


class ToolIconsTest(unittest.TestCase):
    def test_concept_icons_are_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name in ("argocd", "generic-dns", "cpu", "zasper"):
                (Path(directory) / f"{name}.svg").write_text(ORANGE, encoding="utf-8")
            names = [path.stem for path in tool_icons(Path(directory))]
        self.assertEqual(names, ["argocd", "zasper"])


class RingPositionTest(unittest.TestCase):
    def test_hues_walk_the_colour_wheel(self) -> None:
        self.assertLess(ring_position(ORANGE), ring_position(BLUE))

    def test_grey_icons_close_the_ring(self) -> None:
        self.assertLess(ring_position(BLUE), ring_position(BLACK))

    def test_white_detail_does_not_turn_an_icon_grey(self) -> None:
        self.assertEqual(ring_position(BLUE_WITH_WHITE_DETAIL), ring_position(BLUE))

    def test_embedded_png_sets_the_hue(self) -> None:
        self.assertEqual(
            list(png_colours(_png([(37, 99, 235, 255), (255, 0, 0, 0)]))), [(37, 99, 235)]
        )


class PlaceTest(unittest.TestCase):
    def test_ids_and_references_take_the_icon_name(self) -> None:
        icon = (
            '<?xml version="1.0"?><!-- note --><svg width="9" viewBox="0 0 8 8">'
            '<defs><linearGradient id="a"/><style>.b{fill:url(#a)}</style></defs>'
            '<path class="b" fill="url(#a)"/><use href="#a"/></svg>'
        )
        placed = place("cert-manager", icon, 1, 2, 3, 4)
        self.assertTrue(
            placed.startswith(
                '<svg x="1.00" y="2.00" width="3.00" height="4.00" viewBox="0 0 8 8">'
            )
        )
        self.assertIn('id="certmanager-a"', placed)
        self.assertIn(".certmanager-b{fill:url(#certmanager-a)}", placed)
        self.assertIn('class="certmanager-b" fill="url(#certmanager-a)"', placed)
        self.assertIn('href="#certmanager-a"', placed)
        self.assertNotIn("note", placed)

    def test_icon_without_view_box_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmblemError, "broken.*viewBox"):
            place("broken", "<svg><path/></svg>", 0, 0, 1, 1)


class EmblemTest(unittest.TestCase):
    def test_icons_shrink_to_fit_a_fuller_ring(self) -> None:
        few = emblem({f"tool{index}": ORANGE for index in range(10)}, WORDMARK)
        many = emblem({f"tool{index}": ORANGE for index in range(90)}, WORDMARK)
        self.assertIn('width="62.00"', few)
        self.assertNotIn('width="62.00"', many)
        self.assertEqual(many.count("<svg "), 90 + 2)

    def test_committed_logo_matches_its_sources(self) -> None:
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
