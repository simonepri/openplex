#!/usr/bin/env python3
"""Extract digest-pinned OCI container base images declared in MODULE.bazel for license auditing."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "MODULE.bazel"


def main() -> int:
    content = MODULE.read_text()
    blocks = re.findall(r"oci[.]pull\((.*?)\n\)", content, re.DOTALL)
    images: list[str] = []
    for block in blocks:
        image = re.findall(r'^\s*image\s*=\s*"([^"]+)"', block, re.MULTILINE)
        digest = re.findall(r'^\s*digest\s*=\s*"(sha256:[a-f0-9]{64})"', block, re.MULTILINE)
        if len(image) != 1 or len(digest) != 1:
            raise ValueError("each oci.pull must declare one image and one sha256 digest")
        images.append(f"{image[0]}@{digest[0]}")
    if not images:
        raise ValueError("MODULE.bazel contains no workload OCI base images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
