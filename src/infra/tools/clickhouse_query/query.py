"""Executes read-only SQL queries against ClickHouse without pod execution."""

from __future__ import annotations

import argparse
import base64
import contextlib
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(ALTER|DROP|TRUNCATE|INSERT|DELETE|CREATE|RENAME|OPTIMIZE|GRANT|REVOKE|KILL|ATTACH|DETACH)\b",
    re.IGNORECASE,
)

_READONLY_KEYWORDS = re.compile(
    r"^\s*(WITH|SELECT|SHOW|DESCRIBE|DESC|EXPLAIN|EXISTS)\b",
    re.IGNORECASE,
)


def validate_readonly_query(query: str) -> None:
    """Ensure query contains only read-only statements as a client-side guard."""
    stripped = query.strip()
    if not stripped:
        raise ValueError("Query string cannot be empty")

    cleaned = re.sub(r"--.*$", "", stripped, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        raise ValueError("Query contains only comments")

    if not _READONLY_KEYWORDS.search(cleaned):
        raise ValueError(
            f"Query must begin with a read-only keyword (SELECT, SHOW, DESCRIBE, EXPLAIN, WITH, EXISTS): {query[:40]}"
        )

    statements = [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]
    for stmt in statements:
        if not _READONLY_KEYWORDS.search(stmt):
            raise ValueError(f"Statement in multi-statement query is not read-only: {stmt[:40]}")
        match = _FORBIDDEN_KEYWORDS.search(stmt)
        if match:
            raise ValueError(
                f"Query contains potentially destructive keyword '{match.group(1).upper()}': {stmt[:60]}"
            )


def fetch_secret_password(
    namespace: str = "signoz",
    k8s_secret: str | None = None,
    secret_key: str | None = None,
    context: str | None = None,
) -> str:
    """Fetch database credential from Kubernetes Secret at runtime without logging."""
    target_secret = k8s_secret or "signoz-clickhouse"
    target_key = secret_key or "password"
    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    cmd.extend([
        "-n",
        namespace,
        "get",
        f"secret/{target_secret}",
        f"-o=jsonpath={{.data.{target_key}}}",
    ])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        encoded = result.stdout.strip()
        if not encoded:
            return ""
        return base64.b64decode(encoded).decode("utf-8").strip()
    except (subprocess.CalledProcessError, OSError, ValueError):
        return ""


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check if host:port is accepting TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def find_free_port() -> int:
    """Return an available unprivileged local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def port_forward_session(
    namespace: str = "signoz",
    service: str = "clickhouse-coordinator",
    remote_port: int = 8123,
    local_port: int | None = None,
    context: str | None = None,
) -> Generator[int]:
    """Establish a temporary kubectl port-forward session for the duration of a query."""
    if local_port is None:
        local_port = find_free_port()

    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    cmd.extend([
        "-n",
        namespace,
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{remote_port}",
    ])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if is_port_open("127.0.0.1", local_port):
                break
            if proc.poll() is not None:
                _, stderr = proc.communicate()
                raise RuntimeError(f"kubectl port-forward exited prematurely: {stderr.strip()}")
            time.sleep(0.1)
        else:
            proc.terminate()
            raise TimeoutError(
                f"Timed out waiting for port-forward to {namespace}/svc/{service}:{remote_port} on port {local_port}"
            )
        yield local_port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def execute_clickhouse_query(
    query: str,
    url: str = "http://127.0.0.1:8123",
    user: str = "admin",
    password: str = "",
    database: str | None = None,
    output_format: str = "TabSeparated",
    timeout: float = 60.0,
    *,
    enforce_readonly: bool = True,
    bypass_client_guard: bool = False,
) -> str:
    """Send SQL query via ClickHouse HTTP endpoint enforcing server-side readonly=1."""
    if not bypass_client_guard:
        validate_readonly_query(query)

    params: dict[str, str] = {"default_format": output_format}
    if enforce_readonly:
        params["readonly"] = "1"
    if database:
        params["database"] = database

    parsed_url = urllib.parse.urlparse(url)
    existing_params = urllib.parse.parse_qs(parsed_url.query)
    for k, v in existing_params.items():
        if k not in params and v:
            params[k] = v[0]

    query_string = urllib.parse.urlencode(params)
    endpoint = urllib.parse.urlunparse((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path or "/",
        "",
        query_string,
        "",
    ))

    request = urllib.request.Request(
        endpoint,
        data=query.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "X-ClickHouse-User": user,
            "X-ClickHouse-Key": password,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            if isinstance(payload, bytes):
                return payload.decode("utf-8", errors="replace")
            return str(payload)
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ClickHouse HTTP {error.code}: {error_body.strip()}") from error
    except urllib.error.URLError as error:
        # Avoid leaking credentials in URL error representation
        clean_url = urllib.parse.urlunparse((
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path or "/",
            "",
            "",
            "",
        ))
        raise RuntimeError(
            f"Failed to connect to ClickHouse at {clean_url}: {error.reason}"
        ) from error


def _resolve_query_text(args: argparse.Namespace) -> str:
    """Extract query text from arguments or standard input."""
    if args.file:
        return pathlib.Path(args.file).read_text(encoding="utf-8")
    if args.query_flag is not None:
        return str(args.query_flag)
    if args.query is not None:
        return str(args.query)
    if not sys.stdin.isatty():
        return str(sys.stdin.read())
    return ""


def _resolve_service(service: str | None, query: str) -> str:
    """Select appropriate service based on user input and query target."""
    if service is not None:
        return service
    if re.search(r"\bsignoz_\w+", query, re.IGNORECASE):
        return "signoz-clickhouse"
    return "clickhouse-coordinator"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for read-only ClickHouse querying."""
    parser = argparse.ArgumentParser(
        description="Execute read-only SQL queries against ClickHouse without pods/exec."
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="SQL query string to execute",
    )
    parser.add_argument(
        "-q",
        "--query-string",
        dest="query_flag",
        help="SQL query string to execute (alternative to positional argument)",
    )
    parser.add_argument(
        "-f",
        "--file",
        help="Path to SQL file containing query",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CLICKHOUSE_URL", "http://127.0.0.1:8123"),
        help="ClickHouse HTTP endpoint URL (default: $CLICKHOUSE_URL or http://127.0.0.1:8123)",
    )
    parser.add_argument(
        "-u",
        "--user",
        default=os.environ.get("CLICKHOUSE_USER", "admin"),
        help="ClickHouse username (default: admin)",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("CLICKHOUSE_PASSWORD"),
        help="ClickHouse password (default: fetched from Kubernetes Secret signoz-clickhouse)",
    )
    parser.add_argument(
        "-d",
        "--database",
        default=os.environ.get("CLICKHOUSE_DATABASE"),
        help="Default database name (e.g. signoz_logs, signoz_metrics)",
    )
    parser.add_argument(
        "--format",
        default=None,
        help="Output format (TabSeparated, PrettyCompact, JSONEachRow, CSV, etc.)",
    )
    parser.add_argument(
        "--port-forward",
        action="store_true",
        help="Force automatic kubectl port-forward to signoz/clickhouse-coordinator",
    )
    parser.add_argument(
        "--namespace",
        default="signoz",
        help="Kubernetes namespace when port-forwarding (default: signoz)",
    )
    parser.add_argument(
        "--service",
        default=None,
        help="Kubernetes service when port-forwarding (default: clickhouse-coordinator, or signoz-clickhouse for telemetry tables)",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Kubectl context to use for port-forwarding",
    )
    parser.add_argument(
        "--bypass-client-guard",
        action="store_true",
        help="Bypass client-side keyword validation to verify server-side readonly=1 enforcement",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Query timeout in seconds (default: 60)",
    )

    args = parser.parse_args(argv)
    query_text = _resolve_query_text(args)
    if not query_text.strip():
        parser.error("No query provided. Pass query as argument, via -f/--file, or via stdin.")

    fmt = args.format or ("PrettyCompact" if sys.stdout.isatty() else "TabSeparated")
    url = args.url
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8123

    need_port_forward = args.port_forward or (
        url == "http://127.0.0.1:8123" and not is_port_open(host, port, timeout=0.2)
    )

    service = _resolve_service(args.service, query_text)
    password = args.password or fetch_secret_password(
        namespace=args.namespace,
        context=args.context,
    )

    try:
        if need_port_forward:
            with port_forward_session(
                namespace=args.namespace,
                service=service,
                remote_port=8123,
                context=args.context,
            ) as forward_port:
                forward_url = f"http://127.0.0.1:{forward_port}"
                result = execute_clickhouse_query(
                    query=query_text,
                    url=forward_url,
                    user=args.user,
                    password=password,
                    database=args.database,
                    output_format=fmt,
                    timeout=args.timeout,
                    enforce_readonly=True,
                    bypass_client_guard=args.bypass_client_guard,
                )
        else:
            result = execute_clickhouse_query(
                query=query_text,
                url=url,
                user=args.user,
                password=password,
                database=args.database,
                output_format=fmt,
                timeout=args.timeout,
                enforce_readonly=True,
                bypass_client_guard=args.bypass_client_guard,
            )
        sys.stdout.write(result)
        if result and not result.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
