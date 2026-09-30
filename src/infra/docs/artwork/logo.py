#!/usr/bin/env python3
"""Generate the OpenPlex logo: the wordmark in a circle, ringed by the icons of the tools the platform uses."""

from __future__ import annotations

import argparse
import base64
import colorsys
import math
import os
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

ICONS = Path("src/infra/docs/assets/icons")
WORDMARK = Path("src/infra/docs/artwork/wordmark.svg")
LOGO = Path("src/infra/docs/assets/logo.svg")

# Icons that draw a concept in the architecture diagrams rather than a tool.
CONCEPT_PREFIX = "generic-"
CONCEPT_ICONS = frozenset({
    # keep-sorted start
    "chrome",
    "cpu",
    "cron-backup",
    "gpu",
    "person",
    "pvc",
    "terminal",
    "vpa",
    # keep-sorted end
})

INK = "#1c1917"
PAPER = "#fdfbf4"
EDGE = "#c9bfa6"

# Size in pixels when a page shows the logo without sizing it.
DISPLAY_SIZE = 320
CENTRE = 500.0
OUTER_RADIUS = 438.0
INNER_RADIUS = 346.0
MAX_ICON_SIZE = 62.0
# Share of the distance between neighbouring icon centres that one icon fills.
ICON_FILL = 0.71
RULE_GAP = 24.0
WORDMARK_FILL = 0.78
# Below this mean saturation an icon counts as grey and joins the end of the ring.
GREY_LIMIT = 0.12
OPAQUE = 128
WHITE_SATURATION = 0.1
WHITE_VALUE = 0.9
SHORT_HEX = 3

PAINT = re.compile(
    r"(?:fill|stroke|stop-color|flood-color|color)\s*[:=]\s*[\"']?\s*"
    r"(?:#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|rgb\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)\s*\))"
)
EMBEDDED_PNG = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=\s]+)")
PROLOG = re.compile(r"<\?xml.*?\?>|<!DOCTYPE.*?>|<!--.*?-->|<title\b.*?</title>", re.DOTALL)
ROOT = re.compile(r"<svg\b([^>]*)>(.*)</svg>", re.DOTALL)
PLACEMENT = re.compile(r"\s(?:width|height|x|y|role|aria-label)=\"[^\"]*\"")
VIEW_BOX = re.compile(r"viewBox=\"([^\"]*)\"")
STYLE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.DOTALL)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
PNG_GREY, PNG_RGB, PNG_PALETTE, PNG_GREY_ALPHA = 0, 2, 3, 4
FILTER_SUB, FILTER_UP, FILTER_AVERAGE, FILTER_PAETH = 1, 2, 3, 4

type Colour = tuple[int, int, int]


class EmblemError(ValueError):
    """An input file cannot be placed in the logo."""


def tool_icons(icons: Path) -> list[Path]:
    """Return every icon that stands for a tool, in name order."""
    return sorted(
        path
        for path in icons.glob("*.svg")
        if path.stem not in CONCEPT_ICONS and not path.stem.startswith(CONCEPT_PREFIX)
    )


def _paeth(left: int, up: int, corner: int) -> int:
    estimate = left + up - corner
    distances = (abs(estimate - left), abs(estimate - up), abs(estimate - corner))
    return (left, up, corner)[distances.index(min(distances))]


def _unfilter(kind: int, row: bytearray, above: bytes, step: int) -> None:
    for i, value in enumerate(row):
        left = row[i - step] if i >= step else 0
        corner = above[i - step] if i >= step else 0
        if kind == FILTER_SUB:
            predicted = left
        elif kind == FILTER_UP:
            predicted = above[i]
        elif kind == FILTER_AVERAGE:
            predicted = (left + above[i]) // 2
        elif kind == FILTER_PAETH:
            predicted = _paeth(left, above[i], corner)
        else:
            return
        row[i] = (value + predicted) & 0xFF


def _png_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        yield data[offset + 4 : offset + 8], data[offset + 8 : offset + 8 + length]
        offset += length + 12


def png_colours(data: bytes) -> Iterator[Colour]:
    """Yield the colour of every opaque pixel of an 8-bit, non-interlaced PNG."""
    chunks = list(_png_chunks(data))
    header = next(body for kind, body in chunks if kind == b"IHDR")
    width, height, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", header)
    if not data.startswith(PNG_SIGNATURE) or depth != 8 or interlace:
        msg = "embedded PNG must be 8-bit and non-interlaced"
        raise EmblemError(msg)
    palette = b"".join(body for kind, body in chunks if kind == b"PLTE")
    alphas = b"".join(body for kind, body in chunks if kind == b"tRNS")
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    step = PNG_CHANNELS[colour_type]
    stride = width * step + 1
    above = bytes(stride - 1)
    for y in range(height):
        row = bytearray(raw[y * stride + 1 : (y + 1) * stride])
        _unfilter(raw[y * stride], row, above, step)
        above = bytes(row)
        for x in range(0, len(row), step):
            pixel = row[x : x + step]
            if colour_type == PNG_PALETTE:
                entry = pixel[0] * 3
                if pixel[0] >= len(alphas) or alphas[pixel[0]] >= OPAQUE:
                    yield (palette[entry], palette[entry + 1], palette[entry + 2])
            elif colour_type in {PNG_GREY, PNG_GREY_ALPHA}:
                if colour_type == PNG_GREY or pixel[1] >= OPAQUE:
                    yield (pixel[0], pixel[0], pixel[0])
            elif colour_type == PNG_RGB or pixel[3] >= OPAQUE:
                yield (pixel[0], pixel[1], pixel[2])


def icon_colours(svg: str) -> Iterator[Colour]:
    """Yield every paint an icon declares and every opaque pixel it embeds."""
    for match in PAINT.finditer(svg):
        digits, red, green, blue = match.groups()
        if digits is None:
            yield (int(red), int(green), int(blue))
            continue
        if len(digits) == SHORT_HEX:
            digits = "".join(digit * 2 for digit in digits)
        yield (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
    for match in EMBEDDED_PNG.finditer(svg):
        yield from png_colours(base64.b64decode(match.group(1)))


def ring_position(svg: str) -> tuple[int, float]:
    """Return the sort key that walks the colour wheel and puts grey icons last."""
    across = up = weight = count = 0.0
    for red, green, blue in icon_colours(svg):
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if saturation < WHITE_SATURATION and value > WHITE_VALUE:
            # White is detail or background; counting it would turn a coloured icon grey.
            continue
        vivid = saturation * value
        across += math.cos(2 * math.pi * hue) * vivid
        up += math.sin(2 * math.pi * hue) * vivid
        weight += vivid
        count += 1
    if count == 0 or weight / count < GREY_LIMIT:
        return (1, 0.0)
    return (0, round(math.atan2(up, across) / (2 * math.pi) % 1, 6))


def _prefix_references(body: str, prefix: str) -> str:
    body = re.sub(r"\bid=\"([^\"]+)\"", rf'id="{prefix}\1"', body)
    body = re.sub(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", rf"url(#{prefix}\1)", body)
    body = re.sub(r"\bhref=\"#([^\"]+)\"", rf'href="#{prefix}\1"', body)
    body = re.sub(
        r"\bclass=\"([^\"]+)\"",
        lambda match: 'class="' + " ".join(prefix + name for name in match.group(1).split()) + '"',
        body,
    )
    return STYLE.sub(
        lambda match: (
            match.group(1)
            + re.sub(r"\.([A-Za-z_][\w-]*)(?=[^{}]*\{)", rf".{prefix}\1", match.group(2))
            + match.group(3)
        ),
        body,
    )


def place(name: str, svg: str, x: float, y: float, width: float, height: float) -> str:
    """Return the drawing as a nested svg element at the given box, with its ids made unique."""
    root = ROOT.search(PROLOG.sub("", svg))
    if root is None or VIEW_BOX.search(root.group(1)) is None:
        msg = f"{name}: expected one svg element with a viewBox"
        raise EmblemError(msg)
    attributes = PLACEMENT.sub("", root.group(1)).strip()
    prefix = re.sub(r"[^a-z0-9]", "", name.lower()) + "-"
    body = _prefix_references(root.group(2).strip(), prefix)
    box = f'x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}"'
    return f"<svg {box} {attributes}>{body}</svg>"


def _ring(icons: list[tuple[str, str]], radius: float, size: float, turn: float) -> list[str]:
    placed = []
    for index, (name, svg) in enumerate(icons):
        angle = 2 * math.pi * (index + turn) / len(icons) - math.pi / 2
        x = CENTRE + radius * math.cos(angle) - size / 2
        y = CENTRE + radius * math.sin(angle) - size / 2
        placed.append(place(name, svg, x, y, size, size))
    return placed


def _wordmark(svg: str, rule_radius: float) -> str:
    view_box = VIEW_BOX.search(svg)
    if view_box is None:
        msg = "wordmark: expected a viewBox"
        raise EmblemError(msg)
    _, _, across, down = (float(number) for number in view_box.group(1).split())
    width = 2 * rule_radius * WORDMARK_FILL
    height = width * down / across
    placed = place("wordmark", svg, CENTRE - width / 2, CENTRE - height / 2, width, height)
    return placed.replace("<svg ", f'<svg color="{INK}" ', 1)


def emblem(icons: dict[str, str], wordmark: str) -> str:
    """Return the logo for the given icons, keyed by tool name."""
    ordered = sorted(icons.items(), key=lambda item: (ring_position(item[1]), item[0]))
    # Every other icon drops to the inner ring, so both rings walk the colour wheel.
    outer, inner = ordered[0::2], ordered[1::2]
    size = min(MAX_ICON_SIZE, ICON_FILL * 2 * math.pi * INNER_RADIUS / max(len(inner), 1))
    rule_radius = INNER_RADIUS - size / 2 - RULE_GAP
    parts = [
        f"<!-- Shows the OpenPlex logo. Generated from {ICONS} and {WORDMARK}: run `mise run //src/infra:artwork` instead of editing. -->",
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
        f' width="{DISPLAY_SIZE}" height="{DISPLAY_SIZE}" viewBox="0 0 1000 1000">',
        "<title>OpenPlex</title>",
        f'<circle cx="500" cy="500" r="496" fill="{PAPER}" stroke="{EDGE}" stroke-width="4"/>',
        *_ring(outer, OUTER_RADIUS, size, 0),
        *_ring(inner, INNER_RADIUS, size, 0.5),
        f'<circle cx="500" cy="500" r="{rule_radius:.2f}" fill="none" stroke="{INK}" stroke-width="10"/>',
        _wordmark(wordmark, rule_radius),
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def generate(root: Path) -> str:
    """Return the logo built from the icons and wordmark under the repository root."""
    icons = {path.stem: path.read_text(encoding="utf-8") for path in tool_icons(root / ICONS)}
    return emblem(icons, (root / WORDMARK).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Check that the committed logo matches its sources, or rewrite it with --fix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite the logo from its sources")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd())),
        help="repository root",
    )
    arguments = parser.parse_args(argv)
    logo = arguments.root / LOGO
    expected = generate(arguments.root)
    if arguments.fix:
        logo.write_text(expected, encoding="utf-8")
        return 0
    if not logo.is_file() or logo.read_text(encoding="utf-8") != expected:
        sys.stderr.write(f"{LOGO} is stale: run `mise run //src/infra:artwork`\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
