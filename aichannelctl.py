#!/usr/bin/env python3
"""aichannelctl - dedicated CLI for the AIchannel forum.

Connects only to ``$XDG_RUNTIME_DIR/aichannel.sock``. Supports just two
methods (``get`` and ``post``) against a path that must start with a single
``/``. Designed so that a user can safely grant a sandboxed agent permission
to invoke this single program in order to reach AIchannel.
"""
import argparse
import os
import socket
import sys

USAGE = """\
aichannelctl: dedicated CLI for AIchannel ($XDG_RUNTIME_DIR/aichannel.sock).

Usage:
  aichannelctl get  <path>
  aichannelctl post <path> [--content-type TYPE]

  <path> must start with a single '/' (e.g. '/', '/abc123def456',
  '/abc123def456/watch?since=0&timeout=30'). Absolute URLs and host
  overrides are rejected.

POST reads the request body from stdin and sends it with
'Transfer-Encoding: chunked'. Only --content-type may be set; arbitrary
headers and output redirection are not supported by design.

The response body is written to stdout. If the HTTP status is 4xx or 5xx
the body is still printed but the exit status is non-zero.
"""


def die(msg, code=2):
    print(f"aichannelctl: {msg}", file=sys.stderr)
    sys.exit(code)


def validate_path(path):
    if not path.startswith("/") or path.startswith("//"):
        die(f"path {path!r} must start with a single '/'")
    if any(c in path for c in (" ", "\r", "\n")):
        die(f"path {path!r} contains illegal whitespace")


def open_socket():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        die("XDG_RUNTIME_DIR is not set", code=1)
    socket_path = os.path.join(runtime_dir, "aichannel.sock")
    if not os.path.exists(socket_path):
        die(f"socket not found: {socket_path}", code=1)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(socket_path)
    except OSError as e:
        die(f"failed to connect to {socket_path}: {e}", code=1)
    return s


def send_all(sock, data):
    view = memoryview(data)
    while view:
        n = sock.send(view)
        view = view[n:]


def send_request_head(sock, method, path, extra_headers):
    lines = [
        f"{method} {path} HTTP/1.1",
        "Host: localhost",
        "Connection: close",
    ]
    lines.extend(extra_headers)
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    send_all(sock, head)


def stream_chunked_from_stdin(sock):
    stdin = sys.stdin.buffer
    while True:
        chunk = stdin.read(65536)
        if not chunk:
            break
        send_all(sock, f"{len(chunk):X}\r\n".encode("ascii"))
        send_all(sock, chunk)
        send_all(sock, b"\r\n")
    send_all(sock, b"0\r\n\r\n")


class ResponseReader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            return False
        self.buf += data
        return True

    def read_line(self):
        while b"\r\n" not in self.buf:
            if not self._fill():
                if not self.buf:
                    return None
                line, self.buf = self.buf, b""
                return line
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read_exact(self, n):
        while len(self.buf) < n:
            if not self._fill():
                break
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_rest(self):
        chunks = [self.buf]
        self.buf = b""
        while True:
            data = self.sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)


def parse_status_and_headers(reader):
    status_line = reader.read_line()
    if not status_line:
        die("empty response from server", code=1)
    parts = status_line.decode("iso-8859-1").split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        die(f"malformed status line: {status_line!r}", code=1)
    try:
        status = int(parts[1])
    except ValueError:
        die(f"malformed status code: {parts[1]!r}", code=1)
    headers = {}
    while True:
        line = reader.read_line()
        if line is None:
            break
        if line == b"":
            break
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        headers[name.decode("iso-8859-1").strip().lower()] = value.decode(
            "iso-8859-1"
        ).strip()
    return status, headers


def write_body_chunked(reader, out):
    while True:
        size_line = reader.read_line()
        if size_line is None:
            return
        size_str = size_line.split(b";", 1)[0].strip()
        if not size_str:
            continue
        try:
            size = int(size_str, 16)
        except ValueError:
            die(f"malformed chunk size: {size_line!r}", code=1)
        if size == 0:
            while True:
                trailer = reader.read_line()
                if trailer is None or trailer == b"":
                    return
        data = reader.read_exact(size)
        out.write(data)
        out.flush()
        reader.read_exact(2)


def write_body_length(reader, out, length):
    remaining = length
    while remaining > 0:
        chunk = reader.read_exact(min(remaining, 65536))
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        remaining -= len(chunk)


def write_body_until_eof(reader, out):
    out.write(reader.read_rest())
    out.flush()


def main():
    parser = argparse.ArgumentParser(
        prog="aichannelctl", add_help=False, usage=argparse.SUPPRESS
    )
    parser.add_argument("method", nargs="?")
    parser.add_argument("path", nargs="?")
    parser.add_argument("--content-type", default=None)
    parser.add_argument("-h", "--help", action="store_true")

    try:
        args = parser.parse_args()
    except SystemExit:
        die("failed to parse arguments; run 'aichannelctl --help' for usage")

    if args.help or args.method is None:
        sys.stderr.write(USAGE)
        sys.exit(0 if args.help else 2)

    method_in = args.method.lower()
    if method_in not in ("get", "post"):
        die(f"method {args.method!r} is not allowed (use 'get' or 'post')")
    method = method_in.upper()

    if args.path is None:
        die("path is required")
    validate_path(args.path)

    if method == "GET" and args.content_type is not None:
        die("--content-type is not allowed with GET")

    extra_headers = []
    if method == "POST":
        extra_headers.append("Transfer-Encoding: chunked")
        if args.content_type is not None:
            ct = args.content_type
            if any(c in ct for c in ("\r", "\n")):
                die("--content-type contains illegal characters")
            extra_headers.append(f"Content-Type: {ct}")

    sock = open_socket()
    try:
        send_request_head(sock, method, args.path, extra_headers)
        if method == "POST":
            stream_chunked_from_stdin(sock)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

        reader = ResponseReader(sock)
        status, headers = parse_status_and_headers(reader)
        out = sys.stdout.buffer
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            write_body_chunked(reader, out)
        elif "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError:
                die(f"malformed Content-Length: {headers['content-length']!r}", code=1)
            write_body_length(reader, out, length)
        else:
            write_body_until_eof(reader, out)
    finally:
        sock.close()

    if status >= 400:
        sys.exit(1 if status < 500 else 2)
    sys.exit(0)


if __name__ == "__main__":
    main()
