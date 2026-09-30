#!/usr/bin/env python3
"""Answer the Bazel graph questions of the //:check gates with one query.

//:check runs this once before the gates that read its answers start. Every
nested Bazel command waits for the one before it on the shared server, so one
query replaces one per gate. Each answer is a file in CHECK_FACTS_DIR:

- source_files: the labels of kind("source file", deps(//...)).
- labels: every label the query returned; each one names a target that exists.
- dev_tool_isolation: the query dev_tool_isolation runs on its first line,
  followed by the labels that query returns.

A gate that finds no answer, or an answer to a different question, runs its
own query, so a missing or stale file costs time but never changes a finding.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

POLICY = "src/bazel/checks/license_images/policy.json"
INVENTORY = "src/infra/images/workload-images.json"


@dataclass(frozen=True)
class Graph:
    """The targets a query returned and the dependency edges leaving each."""

    source_files: list[str]
    deps: dict[str, list[str]]


def dev_tool_query(images: list[str], locks: list[str]) -> str:
    """Return the query dev_tool_isolation runs, formatted as that gate formats it."""
    lock_labels = " ".join(lock_label(lock) for lock in locks)
    return f"deps(set({''.join(f'{image} ' for image in images)})) intersect set({lock_labels})"


def lock_label(path: str) -> str:
    directory = PurePosixPath(path).parent.as_posix()
    return f"//{'' if directory == '.' else directory}:{PurePosixPath(path).name}"


def graph_query(images: list[str], locks: list[str]) -> str:
    """Return one query whose result answers every question.

    The paths from shipped images to dev-tool locks come without their source
    files, so every source file in the result is one of deps(//...).
    """
    expression = 'kind("source file", deps(//...)) + //...'
    if images and locks:
        image_set = " ".join(images)
        lock_set = " ".join(lock_label(lock) for lock in locks)
        expression += (
            f" + let paths = allpaths(set({image_set}), set({lock_set}))"
            ' in $paths except kind("source file", $paths)'
        )
    return expression


def parse_graph(lines: list[str]) -> Graph:
    """Parse streamed_jsonproto output, which may repeat targets, into source files and edges."""
    source_files: set[str] = set()
    deps: dict[str, list[str]] = {}
    for line in lines:
        if not line.strip():
            continue
        target = json.loads(line)
        kind = target["type"]
        if kind == "SOURCE_FILE":
            name = target["sourceFile"]["name"]
            source_files.add(name)
            deps[name] = []
        elif kind == "GENERATED_FILE":
            deps[target["generatedFile"]["name"]] = [target["generatedFile"]["generatingRule"]]
        elif kind == "RULE":
            deps[target["rule"]["name"]] = list(target["rule"].get("ruleInput", []))
    return Graph(source_files=sorted(source_files), deps=deps)


def reached_locks(graph: Graph, images: list[str], locks: list[str]) -> list[str] | None:
    """Return the locks the images depend on, or None when the graph cannot tell.

    Every path from an image to a lock lies in the result, so a walk from the
    images over the result finds exactly the locks deps(images) contains. The
    walk starts from labels as the inventory spells them, so an image or lock
    spelled differently from Bazel's canonical label leaves the answer open.
    """
    lock_labels = {lock_label(lock) for lock in locks}
    if any(label not in graph.deps for label in [*images, *lock_labels]):
        return None
    reached: set[str] = set()
    seen = set(images)
    pending = list(images)
    while pending:
        for dep in graph.deps[pending.pop()]:
            if dep in lock_labels:
                reached.add(dep)
            if dep in graph.deps and dep not in seen:
                seen.add(dep)
                pending.append(dep)
    return sorted(reached)


def write(directory: Path, name: str, lines: list[str]) -> None:
    """Publish one answer whole, so a reader never sees a partial file."""
    staging = directory / f".{name}.tmp"
    staging.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    staging.replace(directory / name)


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", Path.cwd()))
    facts_dir = Path(os.environ["CHECK_FACTS_DIR"])
    policy = json.loads((root / POLICY).read_text(encoding="utf-8"))
    inventory = json.loads((root / INVENTORY).read_text(encoding="utf-8"))
    locks = [t["path"] for t in policy["sourceScan"]["targets"] if t.get("context") == "dev-tool"]
    images = [image["target"] for image in inventory["images"]]

    output_root = os.environ.get("BAZEL_OUTPUT_ROOT") or str(root / ".tmp/state/bazel")
    query = subprocess.run(
        [
            "bazel",
            f"--output_user_root={output_root}",
            "query",
            *shlex.split(os.environ.get("BAZEL_CONFIG_FLAGS", "")),
            "--output=streamed_jsonproto",
            "--proto:output_rule_attrs=",
            "--order_output=no",
            "--ui_event_filters=-info",
            "--noshow_progress",
            graph_query(images, locks),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if query.returncode != 0:
        print(query.stderr[-2000:], file=sys.stderr)
        return 1

    graph = parse_graph(query.stdout.splitlines())
    write(facts_dir, "source_files", graph.source_files)
    write(facts_dir, "labels", sorted(graph.deps))
    reached = reached_locks(graph, images, locks) if images and locks else None
    if reached is not None:
        write(facts_dir, "dev_tool_isolation", [dev_tool_query(images, locks), *reached])
    return 0


if __name__ == "__main__":
    sys.exit(main())
