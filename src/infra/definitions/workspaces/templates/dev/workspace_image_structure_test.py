"""Verifies multi-architecture developer workspace container images contain required system binaries and helpers."""

from __future__ import annotations

import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

_CLAUDE_CONFIG_PATH = "etc/claude-code/managed-settings.json"
_CODEX_CONFIG_PATH = "etc/codex/managed_config.toml"
_HELPER_PACKAGE = "amazon-ecr-credential-helper"
_HELPER_PATH = "usr/bin/docker-credential-ecr-login"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def main() -> None:
    layouts = {
        "amd64": Path(sys.argv[1]),
        "arm64": Path(sys.argv[2]),
    }
    lock = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    verify_locked_helper(lock)
    for architecture, layout in layouts.items():
        verify_image(layout, architecture)


EXPECTED_ARCHITECTURES_COUNT = 2


def verify_locked_helper(lock: dict[str, Any]) -> None:
    packages = [
        package for package in lock.get("packages", []) if package.get("name") == _HELPER_PACKAGE
    ]
    by_architecture = {package.get("arch"): package for package in packages}
    if set(by_architecture) != {"amd64", "arm64"} or len(packages) != EXPECTED_ARCHITECTURES_COUNT:
        raise RuntimeError("the workspace package lock must pin one ECR helper per architecture")

    versions = {package.get("version") for package in packages}
    if len(versions) != 1 or not next(iter(versions), None):
        raise RuntimeError("both workspace architectures must pin the same ECR helper version")
    for architecture, package in by_architecture.items():
        if not _SHA256.fullmatch(str(package.get("sha256", ""))):
            raise RuntimeError(f"the {architecture} ECR helper must have a SHA-256 lock")


def verify_image(layout: Path, expected_architecture: str) -> None:
    manifest = descriptor_document(layout, index_descriptor(layout))
    config = descriptor_document(layout, manifest["config"])
    if config.get("architecture") != expected_architecture or config.get("os") != "linux":
        raise RuntimeError(f"{expected_architecture} workspace image has the wrong platform")

    image_config = config.get("config", {})
    if image_config.get("User") != "1000:1000":
        raise RuntimeError(f"{expected_architecture} workspace image must run as 1000:1000")
    if image_config.get("WorkingDir") != "/home/coder":
        raise RuntimeError(f"{expected_architecture} workspace image has the wrong work directory")

    environment = dict(
        entry.split("=", 1)
        for entry in image_config.get("Env", [])
        if isinstance(entry, str) and "=" in entry
    )
    if "/usr/bin" not in environment.get("PATH", "").split(":"):
        raise RuntimeError(f"{expected_architecture} workspace PATH cannot find the ECR helper")

    helper = top_layer_member(layout, manifest["layers"], _HELPER_PATH)
    if helper is None or not helper.isfile() or helper.mode & 0o111 == 0:
        raise RuntimeError(
            f"{expected_architecture} workspace image lacks executable /{_HELPER_PATH}"
        )

    for config_path in (_CLAUDE_CONFIG_PATH, _CODEX_CONFIG_PATH):
        file_entry = top_layer_file(layout, manifest["layers"], config_path)
        if file_entry is None or not file_entry[0].isfile():
            raise RuntimeError(f"{expected_architecture} workspace image lacks /{config_path}")
        member, content = file_entry
        if member.mode & 0o777 != 0o444:
            raise RuntimeError(
                f"{expected_architecture} workspace image /{config_path} mode is not 0444"
            )
        if (
            member.uid != 0
            or member.gid != 0
            or (member.uname and member.uname != "root")
            or (member.gname and member.gname != "root")
        ):
            raise RuntimeError(
                f"{expected_architecture} workspace image /{config_path} must be owned by root"
            )
        if config_path == _CLAUDE_CONFIG_PATH:
            claude_env = json.loads(content.decode("utf-8")).get("env", {})
            if str(claude_env.get("CLAUDE_CODE_ENABLE_TELEMETRY")) != "1":
                raise RuntimeError(f"{expected_architecture} Claude settings must enable telemetry")
            if str(claude_env.get("OTEL_LOG_USER_PROMPTS")) != "0":
                raise RuntimeError(
                    f"{expected_architecture} Claude settings must disable user prompt logging"
                )


def index_descriptor(layout: Path) -> dict[str, Any]:
    index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    manifests = index.get("manifests", [])
    if len(manifests) != 1:
        raise RuntimeError("architecture-specific OCI layout must contain one manifest")
    return manifests[0]


def descriptor_document(layout: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("OCI descriptor must use SHA-256")
    path = layout / "blobs" / "sha256" / digest.removeprefix("sha256:")
    return json.loads(path.read_text(encoding="utf-8"))


def top_layer_file(
    layout: Path, layers: list[dict[str, Any]], path: str
) -> tuple[tarfile.TarInfo, bytes] | None:
    """Return the entry and content the image shows at path, reading layers from the top down.

    The highest layer that adds path or whites out path or a parent decides the answer,
    so the large base layers beneath it are never decompressed.
    """
    parents = [str(parent) for parent in Path(path).parents if str(parent) != "."]
    hiding_markers = {f"{parent}/.wh..wh..opq" for parent in parents}
    hiding_markers |= {
        str(Path(hidden).parent / f".wh.{Path(hidden).name}").removeprefix("./")
        for hidden in [path, *parents]
    }
    for layer in reversed(layers):
        digest = layer.get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RuntimeError("OCI layer must use SHA-256")
        blob = layout / "blobs" / "sha256" / digest.removeprefix("sha256:")
        hidden = False
        with tarfile.open(blob, mode="r|*") as archive:
            for member in archive:
                name = member.name.removeprefix("./").lstrip("/")
                if name == path:
                    extracted = archive.extractfile(member) if member.isfile() else None
                    content = extracted.read() if extracted is not None else b""
                    return member, content
                hidden = hidden or name in hiding_markers
        if hidden:
            return None
    return None


def top_layer_member(
    layout: Path, layers: list[dict[str, Any]], path: str
) -> tarfile.TarInfo | None:
    """Return the entry the image shows at path, reading layers from the top down."""
    entry = top_layer_file(layout, layers, path)
    return entry[0] if entry is not None else None


if __name__ == "__main__":
    main()
