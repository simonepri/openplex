#!/usr/bin/env python3
"""Bridges a loopback socket to a CA-verified TLS endpoint."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

BUFFER_SIZE = 64 * 1024
HALF_CLOSE_DRAIN_TIMEOUT = 1.0


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--listen-host", choices=("127.0.0.1", "::1"), required=True)
    argument_parser.add_argument("--listen-port", type=port, required=True)
    argument_parser.add_argument("--upstream-address", required=True)
    argument_parser.add_argument("--upstream-host", required=True)
    argument_parser.add_argument("--upstream-port", type=port, required=True)
    argument_parser.add_argument("--ca-file", type=Path, required=True)
    argument_parser.add_argument("--listen-cert-file", type=Path)
    argument_parser.add_argument("--listen-key-file", type=Path)
    argument_parser.add_argument("--ready-file", type=Path, required=True)
    return argument_parser


def port(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(BUFFER_SIZE):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, TimeoutError, ssl.SSLError, OSError):
        await writer.wait_closed()


def connection_handler(
    context: ssl.SSLContext,
    upstream_address: str,
    upstream_host: str,
    upstream_port: int,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]]:
    async def handle(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                upstream_address,
                upstream_port,
                ssl=context,
                server_hostname=upstream_host,
            )
        except (ConnectionError, OSError, ssl.SSLError):
            await close_writer(client_writer)
            return

        client_to_upstream = asyncio.create_task(copy_stream(client_reader, upstream_writer))
        upstream_to_client = asyncio.create_task(copy_stream(upstream_reader, client_writer))
        transfers = {client_to_upstream, upstream_to_client}
        try:
            done, pending = await asyncio.wait(
                transfers,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for transfer in done:
                with contextlib.suppress(ConnectionError, OSError):
                    await transfer
            if client_to_upstream in done and upstream_to_client in pending:
                with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(upstream_to_client),
                        timeout=HALF_CLOSE_DRAIN_TIMEOUT,
                    )
        finally:
            for transfer in transfers:
                transfer.cancel()
            for transfer in transfers:
                with contextlib.suppress(
                    asyncio.CancelledError, ConnectionError, OSError, ssl.SSLError
                ):
                    await transfer
            await close_writer(upstream_writer)
            await close_writer(client_writer)

    return handle


async def run(arguments: argparse.Namespace) -> None:
    upstream_context = ssl.create_default_context(cafile=str(arguments.ca_file))
    upstream_context.minimum_version = ssl.TLSVersion.TLSv1_2
    listener_context = None
    if arguments.listen_cert_file:
        listener_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        listener_context.minimum_version = ssl.TLSVersion.TLSv1_2
        listener_context.load_cert_chain(
            arguments.listen_cert_file,
            arguments.listen_key_file,
        )
    server = await asyncio.start_server(
        connection_handler(
            upstream_context,
            arguments.upstream_address,
            arguments.upstream_host,
            arguments.upstream_port,
        ),
        arguments.listen_host,
        arguments.listen_port,
        ssl=listener_context,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for watched_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(watched_signal, stop.set)

    arguments.ready_file.write_text(f"{arguments.upstream_host}\n")
    arguments.ready_file.chmod(0o600)
    try:
        async with server:
            await stop.wait()
    finally:
        arguments.ready_file.unlink(missing_ok=True)


def main() -> None:
    arguments = parser().parse_args()
    if not arguments.ca_file.is_file():
        raise SystemExit(f"CA file does not exist: {arguments.ca_file}")
    if bool(arguments.listen_cert_file) != bool(arguments.listen_key_file):
        raise SystemExit("--listen-cert-file and --listen-key-file must be provided together")
    for credential in (arguments.listen_cert_file, arguments.listen_key_file):
        if credential and not credential.is_file():
            raise SystemExit(f"listener credential does not exist: {credential}")
    os.umask(0o077)
    arguments.ready_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    asyncio.run(run(arguments))


if __name__ == "__main__":
    main()
