"""Resolves storage topologies and parses virtual S3 URIs across cluster cells and providers."""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
URI_RE = re.compile(r"^s3://([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)/(home|scratch)/([^/]+)/.+$")


def validate_uri(uri: str, team: str) -> None:
    """Validate that a virtual storage URI belongs to the expected team."""
    match = URI_RE.fullmatch(uri)
    if match is None or match.group(3) != team:
        raise ValueError(
            f"URI must match s3://global/home/{team}/... or s3://cell-name/home/{team}/..."
        )
    if match.group(1) == "global" and match.group(2) != "home":
        raise ValueError("global storage accepts home paths only")


@dataclasses.dataclass(frozen=True)
class ResolvedUri:
    top: str
    cell: str
    remote_path: str
    is_global: bool


@dataclasses.dataclass(frozen=True)
class StorageTopology:
    team: str
    cells: dict[str, str]
    global_writer_cell: str
    global_cells: list[str]
    egress_usd_per_gib: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StorageTopology:
        team = data.get("team", "")
        global_writer = data.get("global-writer-cell", "")
        global_cells = [c.strip() for c in data.get("global-cells", "").splitlines() if c.strip()]
        cells: dict[str, str] = {}
        min_parts = 2
        for raw_line in data.get("cells", "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= min_parts:
                cells[parts[0]] = parts[1]
        egress_str = data.get("egress-usd-per-gib", "")
        egress_cost = float(egress_str) if egress_str else None
        return cls(
            team=team,
            cells=cells,
            global_writer_cell=global_writer,
            global_cells=global_cells,
            egress_usd_per_gib=egress_cost,
        )

    @classmethod
    def from_directory(cls, path: Path) -> StorageTopology:
        data: dict[str, str] = {}
        for child in path.iterdir():
            if child.is_file():
                data[child.name] = child.read_text(encoding="utf-8").strip()
        return cls.from_dict(data)

    def resolve(self, uri: str) -> ResolvedUri:
        validate_uri(uri, self.team)
        rest = uri.removeprefix("s3://")
        top, path = rest.split("/", 1)
        remote = self.cells.get(top)
        if not remote:
            raise ValueError(f"unknown storage name '{top}'")
        is_global = top == "global"
        cell = self.global_writer_cell if is_global else top
        return ResolvedUri(
            top=top,
            cell=cell,
            remote_path=f"{remote}:{path}",
            is_global=is_global,
        )
