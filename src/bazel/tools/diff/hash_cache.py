"""BuildBuddy remote cache transport for bazel-diff hash sharing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path


def resolve_buildbuddy_api_key(repo_root: Path | None = None) -> str | None:
    """Resolve BuildBuddy API key without printing or exposing secrets."""
    if key := os.environ.get("BUILDBUDDY_API_KEY"):
        return key.strip()

    bazelrc = Path.home() / ".bazelrc"
    if bazelrc.is_file():
        try:
            if m := re.search(
                r"--remote_header=x-buildbuddy-api-key=([^\s]+)", bazelrc.read_text("utf-8")
            ):
                raw = m.group(1)
                assert raw is not None
                return str(raw).strip()
        except OSError:
            pass

    root = repo_root or Path.cwd()
    helper = root / "src/bazel/tools/buildbuddy/credential_helper.sh"
    if helper.is_file() and os.access(helper, os.X_OK):
        try:
            res = subprocess.run([str(helper), "get"], capture_output=True, text=True, check=False)
            if (
                res.returncode == 0
                and isinstance(raw := json.loads(res.stdout or "{}"), dict)
                and isinstance(hdr := raw.get("headers"), dict)
                and (val := hdr.get("x-buildbuddy-api-key"))
            ):
                return str(val[0] if isinstance(val, list) else val).strip()
        except (OSError, json.JSONDecodeError):
            pass

    try:
        res = subprocess.run(
            ["git", "config", "buildbuddy.apikey"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except OSError:
        pass
    return None


def encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    buf = bytearray()
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(buf + bytearray([value & 0x7F]))


def encode_field(num: int, val: int | str | bytes) -> bytes:
    """Encode a protobuf field with tag and wire type."""
    if isinstance(val, int):
        return encode_varint(num << 3) + encode_varint(val)
    raw = val.encode("utf-8") if isinstance(val, str) else val
    return encode_varint((num << 3) | 2) + encode_varint(len(raw)) + raw


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    val, shift = 0, 0
    while i < len(data):
        b = data[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, i


def decode_proto(data: bytes) -> dict[int, list[int | bytes]]:
    """Decode flat protobuf fields into mapping of field number to values."""
    fields: dict[int, list[int | bytes]] = {}
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        wire, num = tag & 7, tag >> 3
        if wire == 0:
            val, i = _read_varint(data, i)
            fields.setdefault(num, []).append(val)
        elif wire == 2:
            length, i = _read_varint(data, i)
            fields.setdefault(num, []).append(data[i : i + length])
            i += length
        else:
            break
    return fields


def post_grpc(endpoint: str, payload: bytes, api_key: str) -> tuple[int, bytes]:
    """Execute a gRPC call over HTTP/2 using curl."""
    url = f"https://remote.buildbuddy.io:443/build.bazel.remote.execution.v2.ActionCache/{endpoint}"
    hdr = [
        "-H",
        "content-type: application/grpc",
        "-H",
        "te: trailers",
        "-H",
        f"x-buildbuddy-api-key: {api_key}",
    ]
    cmd = ["curl", "--http2-prior-knowledge", "-s", "-i", *hdr, "--data-binary", "@-", url]
    res = subprocess.run(
        cmd,
        input=b"\x00" + struct.pack(">I", len(payload)) + payload,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        return 1, b""
    status = int(m.group(1)) if (m := re.search(rb"grpc-status:\s*(\d+)", res.stdout)) else 0
    parts = res.stdout.split(b"\r\n\r\n", 1)
    return status, parts[1] if len(parts) > 1 else b""


def _parse_cas_digest(body: bytes) -> tuple[str, int] | None:
    try:
        f = decode_proto(body[5:])[1][0]
        assert isinstance(f, bytes)
        d_raw = decode_proto(f)[2][0]
        assert isinstance(d_raw, bytes)
        d = decode_proto(d_raw)
        h, sz = d[1][0], d[2][0]
        assert isinstance(h, bytes) and isinstance(sz, int)
        return h.decode("utf-8"), sz
    except (KeyError, IndexError, AssertionError):
        return None


def fetch(key: str, repo_root: Path | None = None) -> bytes | None:
    """Fetch hash file payload from BuildBuddy Action Cache and CAS."""
    try:
        if not (api_key := resolve_buildbuddy_api_key(repo_root)):
            return None
        act_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        req = encode_field(2, encode_field(1, act_hash) + encode_field(2, len(key)))
        status, body = post_grpc("GetActionResult", req, api_key)
        if status == 0 and (digest := _parse_cas_digest(body)):
            cas_hash, cas_size = digest
            dl = subprocess.run(
                ["bb", "download", f"{cas_hash}/{cas_size}", f"--api_key={api_key}"],
                capture_output=True,
                check=False,
            )
            if dl.returncode == 0:
                return dl.stdout
            sys.stderr.write(
                f"[WARN] Failed to download {cas_hash[:8]} from CAS (exit {dl.returncode})\n"
            )
    except Exception as exc:
        sys.stderr.write(f"[WARN] Remote cache fetch failed: {exc.__class__.__name__}\n")
    return None


def store(key: str, data: bytes, repo_root: Path | None = None) -> None:
    """Store hash file payload in BuildBuddy CAS and Action Cache."""
    try:
        if not (api_key := resolve_buildbuddy_api_key(repo_root)):
            return
        cas_hash, cas_size = hashlib.sha256(data).hexdigest(), len(data)
        up = subprocess.run(
            ["bb", "upload", "--stdin", f"--digest={cas_hash}/{cas_size}", f"--api_key={api_key}"],
            input=data,
            capture_output=True,
            check=False,
        )
        if up.returncode != 0:
            sys.stderr.write(
                f"[WARN] Failed to upload {cas_hash[:8]} to CAS (exit {up.returncode})\n"
            )
            return
        act_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        dig = encode_field(1, cas_hash) + encode_field(2, cas_size)
        file_pb = encode_field(1, "hashes.json") + encode_field(2, dig)
        req = encode_field(2, encode_field(1, act_hash) + encode_field(2, len(key))) + encode_field(
            3, encode_field(1, file_pb)
        )
        if (status := post_grpc("UpdateActionResult", req, api_key)[0]) != 0:
            sys.stderr.write(f"[WARN] Failed to update Action Cache (status {status})\n")
    except Exception as exc:
        sys.stderr.write(f"[WARN] Remote cache store failed: {exc.__class__.__name__}\n")
