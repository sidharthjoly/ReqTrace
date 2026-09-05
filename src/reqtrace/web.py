"""The thinnest possible UI: a stdlib HTTP server and one static page.

No framework and no build step — there is no Node on this machine, and a job
index for one person does not need a bundler. Two JSON endpoints and a single
vanilla HTML/JS file.

    uv run python -m reqtrace.web          # http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import runs as R
from . import search as S
from .companies import rows as company_rows
from .store import DEFAULT_SQLITE, Store

STATIC = Path(__file__).resolve().parent / "static"


def _bool(v: str | None) -> bool:
    return str(v).lower() in {"1", "true", "yes", "on"}


class Handler(SimpleHTTPRequestHandler):
    db_path = DEFAULT_SQLITE

    def _json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, name="index.html"):
        path = STATIC / name
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}

        if url.path in ("/", "/index.html"):
            return self._page()
        if url.path in ("/runs", "/runs.html"):
            return self._page("runs.html")

        # A connection per request: SQLite objects are not thread-safe and
        # opening the file is cheap.
        if url.path.startswith("/api/"):
            # The scheduled sweep can hold a write for minutes; the store runs
            # in WAL so readers are not blocked, but keep a timeout anyway for
            # the moments WAL still needs the write lock (checkpoint, schema).
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                if url.path == "/api/stats":
                    return self._json(S.stats(conn))
                if url.path == "/api/runs":
                    return self._json(R.health(conn))
                if url.path == "/api/search":
                  try:
                    query = S.Query(
                        q=qs.get("q", ""),
                        city=qs.get("city", ""),
                        country=qs.get("country", "AU"),
                        remote=qs.get("remote", ""),
                        vendor=qs.get("vendor", ""),
                        company=qs.get("company", ""),
                        data_only=_bool(qs.get("data_only")),
                        has_salary=_bool(qs.get("has_salary")),
                        days=int(qs.get("days") or 0),
                        include_closed=_bool(qs.get("include_closed")),
                        sort=qs.get("sort", "newest"),
                        limit=min(int(qs.get("limit") or 50), 200),
                        offset=int(qs.get("offset") or 0),
                    )
                  except (TypeError, ValueError) as e:
                    # A bookmarked URL with a stale param should be a 400, not a
                    # dead handler thread.
                    return self._json({"error": f"bad parameter: {e}"}, 400)
                  res = S.search(conn, query)
                  return self._json({"total": res.total, "results": res.rows,
                                     "facets": res.facets})
            except sqlite3.OperationalError as e:
                return self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return self.send_error(404)

        return self.send_error(404)

    def log_message(self, fmt, *args):  # quieter console
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-reindex", action="store_true")
    args = ap.parse_args()

    if os.environ.get("DATABASE_URL"):
        # Ingestion honours DATABASE_URL, but the query layer does not yet:
        # search.py is FTS5/sqlite3 throughout (? placeholders, datetime('now'),
        # CREATE VIRTUAL TABLE). Say so rather than crashing inside reindex().
        print("DATABASE_URL is set, but the UI is SQLite-only for now.\n"
              "search.py needs a tsvector query path before it can serve "
              "Postgres. Unset DATABASE_URL to browse the local index.")
        return 2

    store = Store()
    store.init_schema()
    store.load_companies(company_rows())
    if not args.no_reindex:
        n = S.reindex(store.conn)
        print(f"search index: {n} rows")
    print("index:", S.stats(store.conn))
    store.close()

    Handler.db_path = DEFAULT_SQLITE
    try:
        server: HTTPServer = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        # A stale server on the port serves *its* build of the page, which looks
        # like a broken deploy rather than a port clash.
        print(f"cannot bind {args.host}:{args.port} — {e}\n"
              f"something else is already serving it; try "
              f"`lsof -ti :{args.port} | xargs kill` or pass --port")
        return 2
    print(f"serving http://{args.host}:{args.port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
