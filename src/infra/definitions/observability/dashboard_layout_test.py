"""Verify dashboard panels fit their grid cells: tables without sideways scrolling, one-row text on one line."""

from __future__ import annotations

import itertools
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

# SigNoz gives a table column roughly 4/3 of a grid unit, so a panel fits 3/4 of a column per unit.
COLUMNS_PER_GRID_UNIT = 0.75
# A one-row text panel holds a single line of this many characters at the dashboard's full width.
ONE_ROW_TEXT_CHARS = 135

_KEYWORD = re.compile(r"\b(SELECT|FROM)\b", re.IGNORECASE)


def select_columns(query: str) -> list[str] | None:
    """Return the outermost SELECT list split into columns, or None for SELECT *."""
    depth = 0
    quote = ""
    selects: list[int] = []
    froms: list[int] = []
    commas: list[int] = []
    for i, char in enumerate(query):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            if char == ",":
                commas.append(i)
            match = _KEYWORD.match(query, i)
            if match and (i == 0 or not (query[i - 1].isalnum() or query[i - 1] == "_")):
                (selects if match.group(1).upper() == "SELECT" else froms).append(i)
    start = selects[-1] + len("SELECT")
    end = next((f for f in froms if f > start), len(query))
    cuts = [start - 1] + [c for c in commas if start < c < end] + [end]
    columns = [query[a + 1 : b].strip() for a, b in itertools.pairwise(cuts)]
    return None if columns == ["*"] else columns


def check(path: Path) -> list[str]:
    spec = json.loads(
        yaml.safe_load(path.read_text(encoding="utf-8"))["spec"]["objectTemplate"]["jsonSpec"]
    )["spec"]
    cells = {
        item["content"]["$ref"].rsplit("/", 1)[-1]: item
        for layout in spec.get("layouts", [])
        for item in layout["spec"]["items"]
    }
    problems = []
    for panel_id, panel in spec["panels"].items():
        cell = cells.get(panel_id)
        plugin = panel["spec"]["plugin"]
        if cell is None:
            continue
        if plugin["kind"] == "signoz/TablePanel":
            query = panel["spec"]["queries"][0]["spec"]["plugin"]["spec"].get("query", "")
            columns = select_columns(query) if query else None
            limit = int(cell["width"] * COLUMNS_PER_GRID_UNIT)
            if columns is not None and len(columns) > limit:
                problems.append(
                    f"{panel_id}: {len(columns)} columns in a width-{cell['width']} panel (max {limit})"
                )
        elif plugin["kind"] == "signoz/TextPanel" and cell["height"] == 1:
            text = plugin["spec"].get("text", "")
            if len(text) > ONE_ROW_TEXT_CHARS or "\n" in text.strip():
                problems.append(
                    f"{panel_id}: {len(text)} characters in a one-row text panel (max {ONE_ROW_TEXT_CHARS}, one line)"
                )
    return problems


class DashboardLayoutTest(unittest.TestCase):
    def test_select_columns_ignores_nested_queries_and_strings(self) -> None:
        query = "WITH a AS (SELECT x, y FROM t) SELECT f(a, 'b, c') AS one, two FROM (SELECT 1, 2) WHERE z IN (1, 2)"
        self.assertEqual(select_columns(query), ["f(a, 'b, c') AS one", "two"])

    def test_panels_fit_their_cells(self) -> None:
        self.assertEqual(check(Path(sys.argv[1])), [])


if __name__ == "__main__":
    unittest.main(argv=sys.argv[:1])
