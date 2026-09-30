#!/usr/bin/env python3
"""Generate the readme overview graphic: clone, then dev workspaces, builds, AI review, and deployments feeding three kinds of workload."""

from __future__ import annotations

import argparse
import html
import os
import sys
from pathlib import Path

OVERVIEW = Path("src/infra/docs/assets/overview.svg")

INK = "#1c1917"
MUTED = "#6f6a63"
PAPER = "#fdfbf4"
CARD = "#ffffff"
EDGE = "#c9bfa6"
WIRE = "#b3aa94"
BLUE = "#477bc5"
ORANGE = "#ea580c"
PURPLE = "#4b3e8e"
GREEN = "#2c9560"
FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace"

WIDTH, HEIGHT = 1280, 480
CARD_W, CARD_H, CARD_Y, GAP = 196, 190, 40, 28
TERMINAL_W = 284
ARROW_W = 44
TEAL = "#0f766e"
CHIP_W, CHIP_H, CHIP_Y = 270, 60, 380
RAIL_Y = 300

STAGES = (
    ("Dev workspaces", BLUE, ("Your editor, terminal, files,", "and agents, in the cloud.")),
    ("Builds", ORANGE, ("Hermetic and remote-cached,", "from laptop to CI.")),
    ("AI review", TEAL, ("Every pull request checked", "against your own rules.")),
    ("Deployments", PURPLE, ("Pull requests promoted", "to every cluster.")),
)
WORKLOADS = ("Services", "Data processing", "ML training & serving")


def _text(
    x: float,
    y: float,
    content: str,
    size: int,
    colour: str,
    weight: int = 400,
    anchor: str = "start",
    family: str = FONT,
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}"'
        f' fill="{colour}" text-anchor="{anchor}">{html.escape(content)}</text>'
    )


def _terminal(x: float) -> list[str]:
    y, h = CARD_Y, CARD_H
    lines = [f'<rect x="{x}" y="{y}" width="{TERMINAL_W}" height="{h}" rx="16" fill="{INK}"/>']
    for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        lines.append(f'<circle cx="{x + 24 + i * 20}" cy="{y + 24}" r="6" fill="{colour}"/>')
    prompt = (
        ("$ git clone", " …/openplex"),
        ("$ mise run", " //src/infra:up"),
    )
    for i, (command, argument) in enumerate(prompt):
        lines.append(
            f'<text x="{x + 24}" y="{y + 76 + i * 34}" font-family="{MONO}" font-size="16" xml:space="preserve">'
            f'<tspan fill="#9ee6b8" font-weight="500">{html.escape(command)}</tspan>'
            f'<tspan fill="{PAPER}">{html.escape(argument)}</tspan></text>'
        )
    lines.append(_text(x + 24, y + 158, "# three clusters, one laptop", 13, "#a8a29e", family=MONO))
    return lines


def _icon(kind: str, x: float, y: float, colour: str) -> list[str]:
    stroke = f'fill="none" stroke="{colour}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"'
    if kind == "Dev workspaces":
        return [
            f'<rect x="{x}" y="{y}" width="34" height="26" rx="5" {stroke}/>',
            f'<line x1="{x}" y1="{y + 8}" x2="{x + 34}" y2="{y + 8}" {stroke}/>',
            f'<path d="M {x + 8} {y + 15} l 5 4 l -5 4 M {x + 17} {y + 23} h 8" {stroke}/>',
        ]
    if kind == "Builds":
        return [
            f'<rect x="{x}" y="{y + 16}" width="16" height="12" rx="3" {stroke}/>',
            f'<rect x="{x + 18}" y="{y + 16}" width="16" height="12" rx="3" {stroke}/>',
            f'<rect x="{x + 9}" y="{y}" width="16" height="12" rx="3" {stroke}/>',
        ]
    if kind == "AI review":
        return [
            f'<rect x="{x}" y="{y}" width="34" height="28" rx="8" {stroke}/>',
            f'<circle cx="{x + 11}" cy="{y + 12}" r="2.5" fill="{colour}"/>',
            f'<circle cx="{x + 23}" cy="{y + 12}" r="2.5" fill="{colour}"/>',
            f'<path d="M {x + 10} {y + 20} q 7 5 14 0" {stroke}/>',
        ]
    return [
        f'<path d="M {x} {y + 14} h 14 m -5 -6 l 6 6 l -6 6" {stroke}/>',
        f'<rect x="{x + 20}" y="{y}" width="12" height="12" rx="3" {stroke}/>',
        f'<rect x="{x + 20}" y="{y + 16}" width="12" height="12" rx="3" {stroke}/>',
    ]


def _card(x: float, title: str, colour: str, copy: tuple[str, str]) -> list[str]:
    y = CARD_Y
    clip = title.lower().replace(" ", "-")
    lines = [
        f'<clipPath id="{clip}"><rect x="{x}" y="{y}" width="{CARD_W}" height="{CARD_H}" rx="16"/></clipPath>',
        f'<rect x="{x}" y="{y}" width="{CARD_W}" height="{CARD_H}" rx="16" fill="{CARD}" stroke="{EDGE}" stroke-width="1.5" filter="url(#shadow)"/>',
        f'<rect x="{x}" y="{y}" width="{CARD_W}" height="6" fill="{colour}" clip-path="url(#{clip})"/>',
        *_icon(title, x + 24, y + 30, colour),
        _text(x + 24, y + 102, title, 23, INK, 600),
    ]
    for i, line in enumerate(copy):
        lines.append(_text(x + 24, y + 130 + i * 22, line, 14, MUTED))
    return lines


def _arrow(x0: float, x1: float, y: float) -> str:
    return f'<path d="M {x0} {y} H {x1 - 2}" fill="none" stroke="{WIRE}" stroke-width="2.5" marker-end="url(#head)"/>'


def _wires(stage_centres: list[float], chip_centres: list[float]) -> list[str]:
    rail_y = RAIL_Y
    left, right = min(stage_centres + chip_centres), max(stage_centres + chip_centres)
    stroke = f'fill="none" stroke="{WIRE}" stroke-width="2.5" stroke-linecap="round"'
    lines = [f'<path d="M {left} {rail_y} H {right}" {stroke}/>']
    for cx in stage_centres:
        lines.extend((
            f'<path d="M {cx} {CARD_Y + CARD_H} V {rail_y}" {stroke}/>',
            f'<circle cx="{cx}" cy="{rail_y}" r="5" fill="{WIRE}"/>',
        ))
    for cx in chip_centres:
        lines.extend((
            f'<path d="M {cx} {rail_y} V {CHIP_Y - 2}" {stroke} marker-end="url(#head)"/>',
            f'<circle cx="{cx}" cy="{rail_y}" r="5" fill="{WIRE}"/>',
        ))
    # The label sits under the rail between the last two drops, where no wire crosses it.
    lx = (chip_centres[-2] + chip_centres[-1]) / 2
    lines.append(_text(lx, rail_y + 28, "wired to work as one", 15, MUTED, 500, "middle"))
    return lines


def _chips() -> tuple[list[str], list[float]]:
    total = 3 * CHIP_W + 2 * GAP
    start = (WIDTH - total) / 2
    lines: list[str] = []
    centres: list[float] = []
    for i, name in enumerate(WORKLOADS):
        x = start + i * (CHIP_W + GAP)
        centres.append(x + CHIP_W / 2)
        lines.extend((
            f'<rect x="{x}" y="{CHIP_Y}" width="{CHIP_W}" height="{CHIP_H}" rx="29" fill="{CARD}" stroke="{EDGE}" stroke-width="1.5" filter="url(#shadow)"/>',
            f'<circle cx="{x + 30}" cy="{CHIP_Y + CHIP_H / 2}" r="7" fill="{GREEN}"/>',
            _text(x + 50, CHIP_Y + CHIP_H / 2 + 6, name, 18, INK, 600),
        ))
    return lines, centres


def overview() -> str:
    """Return the overview graphic."""
    terminal_x = (WIDTH - (TERMINAL_W + len(STAGES) * (CARD_W + ARROW_W))) / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<title>Clone the repository, then dev workspaces, builds, and deployments serve every kind of workload</title>",
        "<defs>",
        '<filter id="shadow" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#1c1917" flood-opacity="0.08"/></filter>',
        f'<marker id="head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 1 1 L 8 5 L 1 9" fill="none" stroke="{WIRE}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></marker>',
        "</defs>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="24" fill="{PAPER}"/>',
        *_terminal(terminal_x),
    ]
    x = terminal_x + TERMINAL_W
    mid_y = CARD_Y + CARD_H / 2
    stage_centres: list[float] = []
    for title, colour, copy in STAGES:
        parts.append(_arrow(x + 8, x + ARROW_W, mid_y))
        x += ARROW_W
        parts.extend(_card(x, title, colour, copy))
        stage_centres.append(x + CARD_W / 2)
        x += CARD_W
    chips, chip_centres = _chips()
    parts.extend(_wires(stage_centres, chip_centres))
    parts.extend(chips)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Check that the committed graphic matches the generator, or rewrite it with --fix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite the graphic")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd())),
        help="repository root",
    )
    arguments = parser.parse_args(argv)
    target = arguments.root / OVERVIEW
    expected = overview()
    if arguments.fix:
        target.write_text(expected, encoding="utf-8")
        return 0
    if not target.is_file() or target.read_text(encoding="utf-8") != expected:
        sys.stderr.write(f"{OVERVIEW} is stale: run `mise run //src/infra:artwork`\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
