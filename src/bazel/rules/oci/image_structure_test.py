"""Validate Ray container image structure and layer archives without running container runtime daemons."""

from __future__ import annotations

import json
import posixpath
import sys
import tarfile
from concurrent.futures import Executor, ProcessPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

_TORCH_METADATA_SUFFIXES = {
    "amd64": "site-packages/torch-2.7.1+cpu.dist-info/METADATA",
    "arm64": "site-packages/torch-2.7.1.dist-info/METADATA",
}

_COMPILER_SUFFIXES = {
    "amd64": "usr/lib/gcc/x86_64-linux-gnu/11/cc1plus",
    "arm64": "usr/lib/gcc/aarch64-linux-gnu/11/cc1plus",
}


MIN_ARGV_WITH_MODULE = 3
_STREAM_BUFFER_BYTES = 1 << 20
# LINT.IfChange(layer-workers)
_LAYER_WORKERS = 4
# LINT.ThenChange(//src/bazel/rules/oci/BUILD.bazel:layer-workers)


class LayerEntry(NamedTuple):
    path: str
    symlink_target: str | None


def main() -> None:
    layouts = {
        "amd64": Path(sys.argv[1]),
        "arm64": Path(sys.argv[2]),
    }
    application_module = (
        sys.argv[3].replace(".", "/") + ".py" if len(sys.argv) > MIN_ARGV_WITH_MODULE else None
    )
    # Reading layer archives dominates the runtime, so layers are read in parallel.
    with ProcessPoolExecutor(max_workers=_LAYER_WORKERS) as executor:
        for architecture, layout in layouts.items():
            verify_layout(executor, layout, architecture, application_module)


def verify_layout(
    executor: Executor, layout: Path, expected_architecture: str, application_module: str | None
) -> None:
    manifest = descriptor_document(layout, index_descriptor(layout))
    config = descriptor_document(layout, manifest["config"])

    if config["architecture"] != expected_architecture:
        raise RuntimeError(
            f"image architecture {config['architecture']} does not match "
            f"expected {expected_architecture}"
        )
    labels = config["config"].get("Labels", {})
    if labels.get("io.ray.ray-version") != "2.58.0":
        raise RuntimeError(f"{expected_architecture} image is not based on Ray 2.58.0")

    paths = layer_paths(executor, layout, manifest["layers"])
    required_suffixes = [
        "usr/bin/bash",
        "site-packages/async_timeout/__init__.py",
        "site-packages/async_timeout-5.0.1.dist-info/METADATA",
        "site-packages/ray/__init__.py",
        "site-packages/ray/serve/__init__.py",
        "site-packages/redis/__init__.py",
        "site-packages/redis-5.2.1.dist-info/METADATA",
        "site-packages/starlette/__init__.py",
        "site-packages/torch/__init__.py",
        _TORCH_METADATA_SUFFIXES[expected_architecture],
        "usr/bin/g++",
        "usr/bin/g++-11",
        "usr/bin/ld",
        "usr/include/c++/11/vector",
        _COMPILER_SUFFIXES[expected_architecture],
    ]
    if application_module:
        required_suffixes.append(f"opt/{application_module}")
    missing = [suffix for suffix in required_suffixes if not contains_suffix(paths, suffix)]
    if missing:
        raise RuntimeError(
            f"{expected_architecture} image lacks runtime paths: {', '.join(missing)}"
        )


def index_descriptor(layout: Path) -> dict[str, Any]:
    index = json.loads((layout / "index.json").read_text())
    manifests = index.get("manifests", [])
    if len(manifests) != 1:
        raise RuntimeError("native OCI layout must contain exactly one manifest")
    return manifests[0]


def descriptor_document(layout: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    digest = descriptor["digest"]
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("OCI descriptor must use sha256")
    return json.loads((layout / "blobs" / "sha256" / digest.removeprefix("sha256:")).read_text())


def layer_paths(executor: Executor, layout: Path, layers: list[dict[str, Any]]) -> set[str]:
    digests = []
    for layer in layers:
        digest = layer["digest"]
        if not isinstance(digest, str):
            raise TypeError("OCI layer has no digest")
        digests.append(digest)
    blobs = [layout / "blobs" / "sha256" / digest.removeprefix("sha256:") for digest in digests]
    paths: set[str] = set()
    aliases: dict[str, str] = {}
    for digest, entries in zip(digests, executor.map(layer_entries, digests, blobs), strict=True):
        members = set()
        for entry in entries:
            if entry.path in {"bin", "lib", "lib64", "sbin"}:
                if entry.path in aliases and entry.symlink_target != aliases[entry.path]:
                    raise RuntimeError(
                        f"OCI layer {digest} replaces base directory alias {entry.path}"
                    )
                if entry.symlink_target is not None:
                    aliases[entry.path] = entry.symlink_target
            if entry.path:
                members.add(entry.path)
        whiteouts = {path for path in members if Path(path).name.startswith(".wh.")}
        for whiteout in whiteouts:
            parent = str(Path(whiteout).parent)
            marker = Path(whiteout).name
            if marker == ".wh..wh..opq":
                prefix = "" if parent == "." else f"{parent}/"
                paths = {path for path in paths if not path.startswith(prefix)}
                continue
            target = str(Path(parent, marker.removeprefix(".wh.")))
            if parent == ".":
                target = marker.removeprefix(".wh.")
            paths = {path for path in paths if path != target and not path.startswith(f"{target}/")}
        paths.update(members - whiteouts)
    return paths


def layer_entries(digest: str, blob: Path) -> list[LayerEntry]:
    """Read one layer archive front to back and verify its member order."""
    with tarfile.open(blob, mode="r|*", bufsize=_STREAM_BUFFER_BYTES) as archive:
        archive_members = list(archive)
    verify_layer_order(digest, archive_members)
    return [
        LayerEntry(normalized_path(member.name), member.linkname if member.issym() else None)
        for member in archive_members
    ]


def verify_layer_order(digest: str, members: list[tarfile.TarInfo]) -> None:
    directory_positions: dict[str, int] = {}
    for position, member in enumerate(members):
        if member.isdir():
            directory_positions.setdefault(normalized_path(member.name), position)

    emitted: dict[str, tarfile.TarInfo] = {}
    for position, member in enumerate(members):
        path = normalized_path(member.name)
        parent = str(PurePosixPath(path).parent)
        while parent != ".":
            prior = emitted.get(parent)
            if prior is not None and not prior.isdir():
                raise RuntimeError(
                    f"OCI layer {digest} defines descendant {path} below non-directory {parent}"
                )
            directory_position = directory_positions.get(parent)
            if directory_position is not None and directory_position > position:
                raise RuntimeError(
                    f"OCI layer {digest} defines directory {parent} after descendant {path}"
                )
            parent = str(PurePosixPath(parent).parent)
        emitted[path] = member


def normalized_path(path: str) -> str:
    normalized = posixpath.normpath(f"/{path}").lstrip("/")
    return "" if normalized == "." else normalized


def contains_suffix(paths: set[str], suffix: str) -> bool:
    return any(path == suffix or path.endswith(f"/{suffix}") for path in paths)


if __name__ == "__main__":
    main()
