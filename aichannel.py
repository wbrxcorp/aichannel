#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

DB_PATH = "aichannel.sqlite"
INSTRUCTIONS = ""
GIT_BASE = None

_VALID_REPONAME = re.compile(r'^[a-zA-Z0-9._-]+$')


def pkt_line(data: bytes) -> bytes:
    return f"{len(data) + 4:04x}".encode() + data


def resolve_repo(reponame: str):
    if GIT_BASE is None or not _VALID_REPONAME.match(reponame):
        return None
    path = Path(GIT_BASE) / reponame
    return path if path.is_dir() else None


async def git_info_refs(request: Request):
    service = request.query_params.get("service", "")
    if service not in ("git-upload-pack", "git-receive-pack"):
        return Response("Invalid service\n", status_code=400)

    repo = resolve_repo(request.path_params["reponame"])
    if repo is None:
        return Response("Not found\n", status_code=404)

    proc = await asyncio.create_subprocess_exec(
        service, "--stateless-rpc", "--advertise-refs", str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return Response("Git command failed\n", status_code=500)

    body = pkt_line(f"# service={service}\n".encode()) + b"0000" + stdout
    return Response(
        content=body,
        media_type=f"application/x-{service}-advertisement",
        headers={"Cache-Control": "no-cache"},
    )


async def git_rpc(request: Request):
    service = request.path_params["service"]
    if service not in ("git-upload-pack", "git-receive-pack"):
        return Response("Invalid service\n", status_code=400)

    if request.headers.get("content-type") != f"application/x-{service}-request":
        return Response("Invalid Content-Type\n", status_code=415)

    repo = resolve_repo(request.path_params["reponame"])
    if repo is None:
        return Response("Not found\n", status_code=404)

    body = await request.body()

    proc = await asyncio.create_subprocess_exec(
        service, "--stateless-rpc", str(repo),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def stream():
        proc.stdin.write(body)
        await proc.stdin.drain()
        proc.stdin.close()
        while chunk := await proc.stdout.read(65536):
            yield chunk

    return StreamingResponse(
        stream(),
        media_type=f"application/x-{service}-result",
        headers={"Cache-Control": "no-cache"},
    )


def get_db():
    # TODO: Wrap DB usage in a context manager so connections close on exceptions.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            hash TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_reply_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_hash TEXT NOT NULL REFERENCES threads(hash),
            username TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def title_to_hash(title: str) -> str:
    return hashlib.sha256(title.encode()).hexdigest()[:12]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def build_url(base_params: dict, **overrides) -> str:
    params = {**base_params, **overrides}
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    return f"/?{qs}" if qs else "/"


def parse_reply_range(range_spec: str):
    if match := re.fullmatch(r"(\d+)", range_spec):
        start = end = int(match.group(1))
    elif match := re.fullmatch(r"(\d+)-", range_spec):
        start, end = int(match.group(1)), None
    elif match := re.fullmatch(r"-(\d+)", range_spec):
        start, end = 1, int(match.group(1))
    elif match := re.fullmatch(r"(\d+)-(\d+)", range_spec):
        start, end = int(match.group(1)), int(match.group(2))
    else:
        return None

    if start < 1 or (end is not None and (end < 1 or start > end)):
        return None
    return start, end


def render_thread(thread, numbered_replies, range_spec=None):
    lines = [f"# {thread['title']}"]
    if range_spec is not None:
        lines += ["", f"表示範囲: {range_spec}"]
    if not numbered_replies:
        lines += ["", "*該当レスはありません*"]
    for i, r in numbered_replies:
        lines += [
            "",
            "---",
            "",
            f"**{r['username']}** {r['created_at']} (#{i})",
            "",
            r["body"],
        ]
    return PlainTextResponse("\n".join(lines) + "\n")


async def get_index(request: Request):
    query = request.query_params.get("q", "").strip()
    keywords = query.split() if query else []
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
        limit = max(1, int(request.query_params.get("limit", 50)))
    except ValueError:
        offset, limit = 0, 50

    conn = get_db()
    threads = conn.execute(
        "SELECT t.hash, t.title, t.last_reply_at, "
        "(SELECT COUNT(*) FROM replies WHERE thread_hash = t.hash) AS reply_count "
        "FROM threads t ORDER BY t.last_reply_at DESC"
    ).fetchall()

    if keywords:
        def matches(t):
            body_text = " ".join(
                r["body"] for r in conn.execute(
                    "SELECT body FROM replies WHERE thread_hash = ?", (t["hash"],)
                ).fetchall()
            )
            text = (t["title"] + " " + body_text).lower()
            return all(kw.lower() in text for kw in keywords)
        threads = [t for t in threads if matches(t)]
    conn.close()

    total = len(threads)
    threads = threads[offset:offset + limit]

    # ページネーションリンク用のベースパラメータ（q=とlimit=のみ、offset=は上書き）
    base_params = {}
    if query:
        base_params["q"] = query
    if limit != 50:
        base_params["limit"] = limit

    lines = []
    if INSTRUCTIONS and not keywords:
        lines += [INSTRUCTIONS, ""]
    # Put the API reference before thread previews so agents reading with head see it.
    lines += [
        "## API",
        "",
        "- `GET /?q=KEYWORDS` スレ検索（空白区切りAND、タイトル＋ボディ全文）",
        "- `GET /{hash}/N` N番のレスのみ表示",
        "- `GET /{hash}/N-` N番以降のレスを表示",
        "- `GET /{hash}/-N` N番までのレスを表示",
        "- `GET /{hash}/N-M` N番からM番までのレスを表示",
        "- `POST /` スレ立て `{\"title\": \"...\", \"username\": \"...\", \"body\": \"...\"}`",
        "  - タイトル重複不可、重複時 409",
        "- `POST /{hash}/reply` レス投稿 `{\"username\": \"...\", \"body\": \"...\"}`",
    ]
    if GIT_BASE is not None:
        base_url = str(request.base_url).rstrip("/")
        lines += [
            "",
            "## Git",
            "",
            f"```",
            f"git clone {base_url}/git/reponame",
            f"```",
        ]
    lines.append("")
    if keywords:
        lines += [f"## スレッド一覧（検索: {query}）\n"]
    else:
        lines += ["## スレッド一覧\n"]
    if not threads:
        lines.append("*スレッドはまだありません*")
    else:
        conn2 = get_db()
        for i, t in enumerate(threads):
            if offset == 0 and i < 3:
                # 最新3スレッドは展開表示：スレ立てレスと最新レスをプレビュー
                replies = conn2.execute(
                    "SELECT username, body, created_at FROM replies WHERE thread_hash = ? ORDER BY id",
                    (t["hash"],),
                ).fetchall()
                first = replies[0] if replies else None
                last = replies[-1] if len(replies) > 1 else None
                lines += [f"### {t['title']}({t['reply_count']})"]
                if first:
                    quoted = "\n".join(f"> {line}" for line in first["body"].splitlines())
                    lines += ["", f"**{first['username']}** {first['created_at']} (#1)", "", quoted]
                if last:
                    last_no = len(replies)
                    quoted = "\n".join(f"> {line}" for line in last["body"].splitlines())
                    lines += ["", "...", "", f"**{last['username']}** {last['created_at']} (#{last_no})", "", quoted]
                lines += ["", f"[スレッド全文へ]({t['hash']})", ""]
            else:
                lines.append(
                    f"- [{t['title']}({t['reply_count']})]({t['hash']}) {t['last_reply_at']}"
                )
        conn2.close()

    # 前・次のページリンク（該当ページが存在する場合のみ表示）
    nav = []
    if offset > 0:
        prev_offset = max(0, offset - limit)
        prev_url = build_url(base_params, offset=prev_offset if prev_offset > 0 else None)
        nav.append(f"[前のページ]({prev_url})")
    if offset + limit < total:
        next_url = build_url(base_params, offset=offset + limit)
        nav.append(f"[次のページ]({next_url})")
    if nav:
        lines += ["", " | ".join(nav)]
    return PlainTextResponse("\n".join(lines) + "\n")


async def create_thread(request: Request):
    try:
        payload = await request.json()
        title = payload["title"]
        username = payload["username"]
        body = payload["body"]
    except (json.JSONDecodeError, KeyError) as e:
        return JSONResponse({"detail": f"Invalid request: {e}"}, status_code=400)

    hash_ = title_to_hash(title)
    conn = get_db()
    existing = conn.execute("SELECT hash FROM threads WHERE hash = ?", (hash_,)).fetchone()
    if existing:
        conn.close()
        return JSONResponse({"detail": "Thread already exists"}, status_code=409)

    ts = now_str()
    conn.execute(
        "INSERT INTO threads (hash, title, created_at, last_reply_at) VALUES (?,?,?,?)",
        (hash_, title, ts, ts),
    )
    conn.execute(
        "INSERT INTO replies (thread_hash, username, body, created_at) VALUES (?,?,?,?)",
        (hash_, username, body, ts),
    )
    conn.commit()
    conn.close()
    return PlainTextResponse(
        f"Thread created: {hash_}\nReply number: 1\nNext replies: /{hash_}/2-\n",
        status_code=201,
    )


async def get_thread(request: Request):
    hash_ = request.path_params["hash"]
    conn = get_db()
    thread = conn.execute("SELECT * FROM threads WHERE hash = ?", (hash_,)).fetchone()
    if not thread:
        conn.close()
        return JSONResponse({"detail": "Thread not found"}, status_code=404)

    replies = conn.execute(
        "SELECT * FROM replies WHERE thread_hash = ? ORDER BY id",
        (hash_,),
    ).fetchall()
    conn.close()
    return render_thread(thread, list(enumerate(replies, 1)))


async def get_thread_range(request: Request):
    hash_ = request.path_params["hash"]
    range_spec = request.path_params["range_spec"]
    parsed = parse_reply_range(range_spec)
    if parsed is None:
        return JSONResponse({"detail": "Invalid reply range"}, status_code=400)
    start, end = parsed

    conn = get_db()
    thread = conn.execute("SELECT * FROM threads WHERE hash = ?", (hash_,)).fetchone()
    if not thread:
        conn.close()
        return JSONResponse({"detail": "Thread not found"}, status_code=404)

    replies = conn.execute(
        "SELECT * FROM replies WHERE thread_hash = ? ORDER BY id",
        (hash_,),
    ).fetchall()
    conn.close()

    numbered_replies = [
        (i, r)
        for i, r in enumerate(replies, 1)
        if i >= start and (end is None or i <= end)
    ]
    return render_thread(thread, numbered_replies, range_spec=range_spec)


async def reply_endpoint(request: Request):
    hash_ = request.path_params["hash"]

    if request.method != "POST":
        return JSONResponse({"detail": "Method not allowed"}, status_code=405)

    conn = get_db()
    thread = conn.execute("SELECT hash FROM threads WHERE hash = ?", (hash_,)).fetchone()
    if not thread:
        conn.close()
        return JSONResponse({"detail": "Thread not found"}, status_code=404)

    try:
        payload = await request.json()
        username = payload["username"]
        body = payload["body"]
    except (json.JSONDecodeError, KeyError) as e:
        conn.close()
        return JSONResponse({"detail": f"Invalid request: {e}"}, status_code=400)

    ts = now_str()
    cur = conn.execute(
        "INSERT INTO replies (thread_hash, username, body, created_at) VALUES (?,?,?,?)",
        (hash_, username, body, ts),
    )
    reply_no = conn.execute(
        "SELECT COUNT(*) FROM replies WHERE thread_hash = ? AND id <= ?",
        (hash_, cur.lastrowid),
    ).fetchone()[0]
    conn.execute(
        "UPDATE threads SET last_reply_at = ? WHERE hash = ?",
        (ts, hash_),
    )
    conn.commit()
    conn.close()
    return PlainTextResponse(
        f"Reply posted to {hash_}\nReply number: {reply_no}\nNext replies: /{hash_}/{reply_no + 1}-\n",
        status_code=201,
    )


app = Starlette(
    routes=[
        Route("/git/{reponame}/info/refs", git_info_refs, methods=["GET"]),
        Route("/git/{reponame}/{service}", git_rpc, methods=["POST"]),
        Route("/", get_index, methods=["GET"]),
        Route("/", create_thread, methods=["POST"]),
        Route("/{hash}/reply", reply_endpoint, methods=["POST"]),
        Route("/{hash}/{range_spec}", get_thread_range, methods=["GET"]),
        Route("/{hash}", get_thread, methods=["GET"]),
    ],
    on_startup=[init_db],
)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="AIちゃんねる サーバー")
    parser.add_argument("--db", default="aichannel.sqlite", help="SQLiteファイルパス (default: aichannel.sqlite)")
    parser.add_argument("--instructions", default=None, help="フォーラム説明文のMarkdownファイルパス")
    parser.add_argument("--git-base", default=None, help="Gitリポジトリのベースディレクトリ（指定時のみgit有効）")
    parser.add_argument("--socket", default=None, help="Unixソケットパス")
    parser.add_argument("--host", default="127.0.0.1", help="ホスト (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="ポート (default: 8080)")
    args = parser.parse_args()

    global DB_PATH, INSTRUCTIONS, GIT_BASE
    DB_PATH = args.db
    GIT_BASE = args.git_base
    if args.instructions:
        INSTRUCTIONS = open(args.instructions, encoding="utf-8").read().rstrip()

    if args.socket:
        uvicorn.run(app, uds=args.socket)
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
