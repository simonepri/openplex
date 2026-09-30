#!/usr/bin/env python3
"""Enforce maximum function line count limits across Python, Go, and Shell source files in the repository."""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

MAX_FUNCTION_LINES = 120

GO_FUNC_PATTERN = re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\(")
BASH_FUNC_PATTERN = re.compile(r"^(?:function\s+)?([a-zA-Z_0-9]+)\s*\(\)\s*\{")


def is_test_file(path: str) -> bool:
    """Returns True if the given file path is a test fixture or suite."""
    name = Path(path).name
    if name.endswith(("_test.go", "_test.py", "_test.ts", "_test.sh", ".test.ts", ".test.js")):
        return True
    if name.startswith("test_"):
        return True
    return bool(
        "testing/acceptance" in path
        or "definitions/conformance" in path
        or "testing/chainsaw" in path
        or "testdata" in path
    )


def find_long_go_functions(
    content: str, max_lines: int = MAX_FUNCTION_LINES
) -> list[tuple[str, int, int]]:
    """Identifies Go functions exceeding max_lines."""
    lines = content.splitlines()
    violations: list[tuple[str, int, int]] = []
    for i, line in enumerate(lines):
        match = GO_FUNC_PATTERN.match(line)
        if not match:
            continue
        name = match.group(1)
        open_braces = 0
        started = False
        end_line = i
        for j in range(i, len(lines)):
            cur = lines[j]
            open_braces += cur.count("{") - cur.count("}")
            if "{" in cur:
                started = True
            if started and open_braces == 0:
                end_line = j
                break
        length = end_line - i + 1
        if length > max_lines:
            violations.append((name, i + 1, length))
    return violations


def find_long_python_functions(
    content: str, filename: str, max_lines: int = MAX_FUNCTION_LINES
) -> list[tuple[str, int, int]]:
    """Identifies Python or Starlark functions exceeding max_lines."""
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return []

    violations: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is None:
            continue
        length = end_lineno - node.lineno + 1
        if length > max_lines:
            violations.append((node.name, node.lineno, length))
    return violations


def find_long_bash_functions(
    content: str, max_lines: int = MAX_FUNCTION_LINES
) -> list[tuple[str, int, int]]:
    """Identifies Bash functions exceeding max_lines."""
    lines = content.splitlines()
    violations: list[tuple[str, int, int]] = []
    for i, line in enumerate(lines):
        match = BASH_FUNC_PATTERN.match(line)
        if not match:
            continue
        name = match.group(1)
        open_braces = 0
        end_line = i
        for j in range(i, len(lines)):
            cur = lines[j]
            open_braces += cur.count("{") - cur.count("}")
            if open_braces == 0:
                end_line = j
                break
        length = end_line - i + 1
        if length > max_lines:
            violations.append((name, i + 1, length))
    return violations


def scan_file(path: Path, max_lines: int = MAX_FUNCTION_LINES) -> list[tuple[Path, str, int, int]]:
    """Scans a single production file for functions exceeding max_lines."""
    path_str = str(path)
    if is_test_file(path_str):
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    violations: list[tuple[Path, str, int, int]] = []
    if path_str.endswith(".go"):
        for name, line, length in find_long_go_functions(content, max_lines):
            violations.append((path, name, line, length))
    elif path_str.endswith((".py", ".bzl")):
        for name, line, length in find_long_python_functions(content, path_str, max_lines):
            violations.append((path, name, line, length))
    elif path_str.endswith(".sh"):
        for name, line, length in find_long_bash_functions(content, max_lines):
            violations.append((path, name, line, length))

    return violations


def scan_directory(
    root: Path, max_lines: int = MAX_FUNCTION_LINES
) -> list[tuple[Path, str, int, int]]:
    """Recursively scans directory for functions exceeding max_lines."""
    violations: list[tuple[Path, str, int, int]] = []
    excluded_parts = {"node_modules", ".tmp", "dist", "build"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in path.parts for part in excluded_parts):
            continue
        violations.extend(scan_file(path, max_lines))
    return violations


def _collect_violations(
    paths: list[str], root: Path, max_lines: int
) -> list[tuple[Path, str, int, int]]:
    if not paths:
        src_dir = root / "src"
        return scan_directory(src_dir, max_lines) if src_dir.is_dir() else []
    violations: list[tuple[Path, str, int, int]] = []
    for p in paths:
        resolved = root / p
        if resolved.is_file():
            violations.extend(scan_file(resolved, max_lines))
        elif resolved.is_dir():
            violations.extend(scan_directory(resolved, max_lines))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Check function length limits across source code")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=MAX_FUNCTION_LINES,
        help=f"Maximum allowed lines per function (default: {MAX_FUNCTION_LINES})",
    )
    parser.add_argument("paths", nargs="*", help="Specific files or directories to scan")
    args = parser.parse_args(argv)

    workspace_root_env = os.getenv("BUILD_WORKSPACE_DIRECTORY")
    workspace_root = Path(workspace_root_env) if workspace_root_env else Path.cwd()

    violations = _collect_violations(args.paths, workspace_root, args.max_lines)

    if violations:
        for path, name, line, length in violations:
            try:
                rel_path = path.relative_to(workspace_root)
            except ValueError:
                rel_path = path
            sys.stderr.write(
                f"{rel_path}:{line}: function '{name}' exceeds length limit ({length} > {args.max_lines} lines)\n"
            )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
