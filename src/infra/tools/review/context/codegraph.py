"""Interfaces with CodeGraph indexing tools to analyze affected code symbols, call hierarchies, and test linkages."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclasses.dataclass(frozen=True)
class SymbolImpact:
    """Impacted symbol discovered by CodeGraph."""

    symbol: str
    file_path: str
    line: int
    kind: str


@dataclasses.dataclass(frozen=True)
class CodeGraphResult:
    """CodeGraph context and impact analysis output."""

    affected_tests: list[str]
    impacted_symbols: list[SymbolImpact]
    context_markdown: str


def _find_codegraph_cmd() -> list[str]:
    """Finds codegraph binary or mise invocation."""
    if shutil.which("codegraph"):
        return ["codegraph"]
    return ["mise", "x", "npm:@colbymchenry/codegraph@1.6.0", "--", "codegraph"]


def ensure_codegraph_initialized(repo_root: Path) -> None:
    """Ensures CodeGraph is initialized in repo_root or restores cache."""
    cg_dir = repo_root / ".codegraph"
    if cg_dir.exists() and (cg_dir / "codegraph.db").exists():
        # Incremental sync
        cmd = [*_find_codegraph_cmd(), "sync", str(repo_root)]
        subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        return

    # Check if a Bazel-cached codegraph.db exists
    bazel_cached_db = repo_root / "bazel-bin/src/infra/tools/review/codegraph.db"
    if bazel_cached_db.exists():
        cg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bazel_cached_db, cg_dir / "codegraph.db")
        cmd = [*_find_codegraph_cmd(), "sync", str(repo_root)]
        subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        return

    # Otherwise init
    cmd = [*_find_codegraph_cmd(), "init", "-y", str(repo_root)]
    subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)


def analyze_codegraph(
    repo_root: Path,
    changed_files: list[str] | None = None,
    changed_symbols: list[str] | None = None,
) -> CodeGraphResult:
    """Queries CodeGraph for affected tests and impacted symbols for changed files/symbols."""
    ensure_codegraph_initialized(repo_root)

    cmd_base = _find_codegraph_cmd()

    # 1. Affected tests
    affected_tests: list[str] = []
    if changed_files:
        aff_cmd = [*cmd_base, "affected", "-p", str(repo_root), "-j", *changed_files]
        res = subprocess.run(aff_cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
                if isinstance(data, list):
                    affected_tests = [str(x) for x in data]
                elif isinstance(data, dict):
                    raw_affected = data.get("affected", [])
                    if isinstance(raw_affected, list):
                        affected_tests = [str(x) for x in raw_affected]
            except Exception:
                pass

    # 2. Impacted symbols
    impacted: list[SymbolImpact] = []
    symbols_to_query = changed_symbols or []
    for sym in symbols_to_query[:5]:  # Bound query count
        imp_cmd = [*cmd_base, "impact", "-p", str(repo_root), "-j", sym]
        res = subprocess.run(imp_cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
                for item in data.get("affected", []):
                    impacted.append(
                        SymbolImpact(
                            symbol=item.get("name", sym),
                            file_path=item.get("path", ""),
                            line=item.get("line", 0),
                            kind=item.get("kind", "symbol"),
                        )
                    )
            except Exception:
                pass

    # 3. CodeGraph context summary
    context_md = ""
    if changed_files:
        ctx_cmd = [
            *cmd_base,
            "context",
            "-p",
            str(repo_root),
            "-f",
            "markdown",
            "--no-code",
            *changed_files[:5],
        ]
        res = subprocess.run(ctx_cmd, cwd=repo_root, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            context_md = res.stdout

    return CodeGraphResult(
        affected_tests=affected_tests,
        impacted_symbols=impacted,
        context_markdown=context_md,
    )


get_codegraph_analysis = analyze_codegraph
