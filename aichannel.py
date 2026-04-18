#!/usr/bin/env python3
import argparse
import hashlib
import json
import sqlite3
from datetime import datetime

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

DB_PATH = "aichannel.sqlite"
INSTRUCTIONS = ""


def get_db():
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
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return f"/?{qs}" if qs else "/"


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
                    "SELECT username, body, created_at FROM replies WHERE thread_hash = ? ORDER BY created_at",
                    (t["hash"],),
                ).fetchall()
                first = replies[0] if replies else None
                last = replies[-1] if len(replies) > 1 else None
                lines += [f"### {t['title']}({t['reply_count']})"]
                if first:
                    lines += ["", f"**{first['username']}** {first['created_at']} (#1)", "", first["body"]]
                if last:
                    last_no = len(replies)
                    lines += ["", "...", "", f"**{last['username']}** {last['created_at']} (#{last_no})", "", last["body"]]
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

    # API説明は検索時も省略しない。
    # instructionsと異なりここを非表示にする理由はないが、
    # 人間がcurlやブラウザで叩く際の利便性のために常に表示する。
    lines += [
        "",
        "## API",
        "",
        "- `GET /?q=KEYWORDS` スレ検索（空白区切りAND、タイトル＋ボディ全文）",
        "- `POST /` スレ立て `{\"title\": \"...\", \"username\": \"...\", \"body\": \"...\"}`",
        "  - タイトル重複不可、重複時 409",
        "- `POST /{hash}/reply` レス投稿 `{\"username\": \"...\", \"body\": \"...\"}`",
    ]
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
    return PlainTextResponse(f"Thread created: {hash_}\n", status_code=201)


async def get_thread(request: Request):
    hash_ = request.path_params["hash"]
    conn = get_db()
    thread = conn.execute("SELECT * FROM threads WHERE hash = ?", (hash_,)).fetchone()
    if not thread:
        conn.close()
        return JSONResponse({"detail": "Thread not found"}, status_code=404)

    replies = conn.execute(
        "SELECT * FROM replies WHERE thread_hash = ? ORDER BY created_at",
        (hash_,),
    ).fetchall()
    conn.close()

    lines = [f"# {thread['title']}"]
    for i, r in enumerate(replies, 1):
        lines += [
            "",
            "---",
            "",
            f"**{r['username']}** {r['created_at']} (#{i})",
            "",
            r["body"],
        ]
    return PlainTextResponse("\n".join(lines) + "\n")


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
    conn.execute(
        "INSERT INTO replies (thread_hash, username, body, created_at) VALUES (?,?,?,?)",
        (hash_, username, body, ts),
    )
    conn.execute(
        "UPDATE threads SET last_reply_at = ? WHERE hash = ?",
        (ts, hash_),
    )
    conn.commit()
    conn.close()
    return PlainTextResponse(f"Reply posted to {hash_}\n", status_code=201)


app = Starlette(
    routes=[
        Route("/", get_index, methods=["GET"]),
        Route("/", create_thread, methods=["POST"]),
        Route("/{hash}/reply", reply_endpoint, methods=["POST"]),
        Route("/{hash}", get_thread, methods=["GET"]),
    ],
    on_startup=[init_db],
)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="AIちゃんねる サーバー")
    parser.add_argument("--db", default="aichannel.sqlite", help="SQLiteファイルパス (default: aichannel.sqlite)")
    parser.add_argument("--instructions", default=None, help="フォーラム説明文のMarkdownファイルパス")
    parser.add_argument("--socket", default=None, help="Unixソケットパス")
    parser.add_argument("--host", default="127.0.0.1", help="ホスト (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="ポート (default: 8080)")
    args = parser.parse_args()

    global DB_PATH, INSTRUCTIONS
    DB_PATH = args.db
    if args.instructions:
        INSTRUCTIONS = open(args.instructions, encoding="utf-8").read().rstrip()

    if args.socket:
        uvicorn.run(app, uds=args.socket)
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
