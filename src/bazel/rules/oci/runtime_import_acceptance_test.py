"""Pull built OCI layouts into Docker through a loopback registry and run acceptance programs in them."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_DIGEST_PREFIX = "sha256:"
_ARGS_PER_IMAGE = 3


def main() -> None:
    acceptance_program = Path(sys.argv[1])
    application_module = sys.argv[2]
    images = sys.argv[3:]
    if not images or len(images) % _ARGS_PER_IMAGE:
        raise SystemExit("expected <layout> <tag> <platform> triples after the module")
    layouts = [Path(layout) for layout in images[0::_ARGS_PER_IMAGE]]

    server = ThreadingHTTPServer(("127.0.0.1", 0), _registry_handler(layouts))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for index in range(0, len(images), _ARGS_PER_IMAGE):
            layout, tag, platform = images[index : index + _ARGS_PER_IMAGE]
            _pull(server.server_address[1], Path(layout), tag)
            _run(tag, platform, acceptance_program, application_module)
    finally:
        server.shutdown()


def _pull(port: int, layout: Path, tag: str) -> None:
    index = json.loads((layout / "index.json").read_text())
    manifests = index.get("manifests", [])
    if len(manifests) != 1:
        raise RuntimeError(f"{layout} must contain exactly one image manifest")
    repository = tag.split(":", 1)[0]
    reference = f"127.0.0.1:{port}/{repository}@{manifests[0]['digest']}"
    subprocess.run(["docker", "pull", "--quiet", reference], check=True)
    subprocess.run(["docker", "tag", reference, tag], check=True)
    # Drop the per-run registry name; the staging tag keeps the layers.
    subprocess.run(["docker", "image", "rm", reference], check=True, stdout=subprocess.DEVNULL)


def _run(tag: str, platform: str, acceptance_program: Path, application_module: str) -> None:
    with acceptance_program.open("rb") as program:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--platform",
                platform,
                "--entrypoint",
                "/bin/bash",
                tag,
                "-c",
                'exec /home/ray/anaconda3/bin/python - "$@"',
                "--",
                application_module,
            ],
            stdin=program,
            check=True,
        )


def _registry_handler(layouts: list[Path]) -> type[BaseHTTPRequestHandler]:
    class RegistryHandler(BaseHTTPRequestHandler):
        """Serve the read-only subset of the OCI distribution API that `docker pull` uses."""

        def do_GET(self) -> None:
            self._respond(send_body=True)

        def do_HEAD(self) -> None:
            self._respond(send_body=False)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _respond(self, *, send_body: bool) -> None:
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if parts == ["v2"]:
                self._send(HTTPStatus.OK, b"{}", "application/json", send_body=send_body)
                return
            if len(parts) < 4 or parts[0] != "v2" or parts[-2] not in {"manifests", "blobs"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            blob = _find_blob(layouts, parts[-1])
            if blob is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            media_type = "application/octet-stream"
            if parts[-2] == "manifests":
                media_type = json.loads(blob.read_bytes()).get(
                    "mediaType", "application/vnd.oci.image.manifest.v1+json"
                )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(blob.stat().st_size))
            self.send_header("Docker-Content-Digest", parts[-1])
            self.end_headers()
            if send_body:
                with blob.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=1 << 20)

        def _send(
            self, status: HTTPStatus, body: bytes, media_type: str, *, send_body: bool
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

    return RegistryHandler


def _find_blob(layouts: list[Path], digest: str) -> Path | None:
    if not digest.startswith(_DIGEST_PREFIX):
        return None
    name = digest.removeprefix(_DIGEST_PREFIX)
    if not name.isalnum():
        return None
    for layout in layouts:
        blob = layout / "blobs" / "sha256" / name
        if blob.is_file():
            return blob
    return None


if __name__ == "__main__":
    main()
