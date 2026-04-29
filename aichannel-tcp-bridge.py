#!/usr/bin/env python3
"""aichannel-tcp-bridge - expose the AIchannel UNIX socket over TCP.

Listens on a TCP socket (default ``[::]:0`` — any address, random port) and
forwards every accepted connection to the AIchannel UNIX socket
(default ``$XDG_RUNTIME_DIR/aichannel.sock``).

Useful when you need to let another host (or container) reach the local
AIchannel daemon temporarily without setting up nginx/socat ad-hoc.

Security note: this exposes the socket *without authentication*. Bind to a
trusted interface (e.g. ``--bind 127.0.0.1`` or a VPN address) and stop the
bridge as soon as you no longer need it.
"""
import argparse
import asyncio
import os
import signal
import socket
import sys


BUF_SIZE = 65536


def default_socket_path():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return None
    return os.path.join(runtime_dir, "aichannel.sock")


async def pump(reader, writer):
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def handle_client(client_reader, client_writer, socket_path, verbose):
    peer = client_writer.get_extra_info("peername")
    if verbose:
        print(f"[bridge] accepted {peer}", file=sys.stderr, flush=True)
    try:
        unix_reader, unix_writer = await asyncio.open_unix_connection(socket_path)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        print(
            f"[bridge] failed to connect to {socket_path}: {e}",
            file=sys.stderr,
            flush=True,
        )
        client_writer.close()
        try:
            await client_writer.wait_closed()
        except OSError:
            pass
        return

    try:
        await asyncio.gather(
            pump(client_reader, unix_writer),
            pump(unix_reader, client_writer),
        )
    finally:
        for w in (unix_writer, client_writer):
            try:
                w.close()
            except OSError:
                pass
        for w in (unix_writer, client_writer):
            try:
                await w.wait_closed()
            except OSError:
                pass
        if verbose:
            print(f"[bridge] closed {peer}", file=sys.stderr, flush=True)


def format_sockname(sock):
    family = sock.family
    name = sock.getsockname()
    if family == socket.AF_INET6:
        host, port, *_ = name
        return f"[{host}]:{port}"
    if family == socket.AF_INET:
        host, port = name
        return f"{host}:{port}"
    return str(name)


async def run(bind, port, socket_path, verbose):
    if not os.path.exists(socket_path):
        print(
            f"aichannel-tcp-bridge: socket not found: {socket_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, socket_path, verbose),
        host=bind,
        port=port,
        reuse_address=True,
    )

    addrs = ", ".join(format_sockname(s) for s in server.sockets)
    print(
        f"aichannel-tcp-bridge: listening on {addrs} -> {socket_path}",
        file=sys.stderr,
        flush=True,
    )

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def request_stop():
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request_stop))

    try:
        await stop
    finally:
        server.close()
        try:
            await server.wait_closed()
        except OSError:
            pass

    print("aichannel-tcp-bridge: shutting down", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(
        prog="aichannel-tcp-bridge",
        description="Expose the AIchannel UNIX socket over TCP (no auth).",
    )
    parser.add_argument(
        "--bind",
        default="::",
        help="address to bind (default: '::', i.e. all IPv4+IPv6 interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port (default: 0 = pick a random free port)",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="path to the AIchannel UNIX socket "
        "(default: $XDG_RUNTIME_DIR/aichannel.sock)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="log each accepted/closed connection to stderr",
    )
    args = parser.parse_args()

    socket_path = args.socket or default_socket_path()
    if not socket_path:
        print(
            "aichannel-tcp-bridge: --socket not given and XDG_RUNTIME_DIR is not set",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        asyncio.run(run(args.bind, args.port, socket_path, args.verbose))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
