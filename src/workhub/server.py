"""Local web board: drag-and-drop lanes on top of the store.

Listens on 127.0.0.1 only. Every request must name localhost in its Host header (this blocks
DNS rebinding), and writes must carry `X-Workhub: 1` with a JSON body, which a page on another
site cannot send without a CORS preflight that this server never approves.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import sync
from .config import Config
from .opener import open_url
from .store import LANES, Store, is_http, split_title_url

log = logging.getLogger(__name__)

APP_ID = "workhub"
STATIC = Path(__file__).with_name("static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 64 * 1024
MAX_NOTE = 2000
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def board_state(store: Store, cfg: Config) -> dict:
    nxt = store.next_item()
    return {
        "lanes": [{"key": key, "name": name} for key, name in LANES],
        "items": store.items(),
        "next": nxt["id"] if nxt else None,
        "sources": store.sources(),
        # An app window opens links through the server (so they land in your default browser);
        # a normal browser tab opens them itself.
        "links": "server" if cfg.board.window == "app" else "browser",
    }


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.stopping = threading.Event()
        port = cfg.board.port
        self.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        super().__init__(("127.0.0.1", port), Handler)

    def sync_loop(self) -> None:
        """Sync at start, then every sync_minutes, until the server stops."""
        interval = self.cfg.board.sync_minutes * 60
        wait = 0.0
        while not self.stopping.wait(wait):
            try:
                report = sync.run(self.cfg, self.store)
                log.info("Background sync: %s", json.dumps(report))
            except Exception:
                log.exception("Background sync failed")
            if interval <= 0:
                return
            wait = float(interval)

    def stop(self) -> None:
        self.stopping.set()
        self.shutdown()


class Handler(BaseHTTPRequestHandler):
    server: BoardServer
    server_version = "workhub"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - keep the log to errors only
        pass

    def do_GET(self) -> None:
        try:
            if not self._host_ok():
                return
            path = urlsplit(self.path).path
            if path in FILES:
                name, content_type = FILES[path]
                return self._send(200, (STATIC / name).read_bytes(), content_type)
            if path == "/api/ping":
                return self._json({"app": APP_ID, "pid": os.getpid()})
            if path == "/api/state":
                return self._json(board_state(self.server.store, self.server.cfg))
            self._json({"error": "Not found"}, 404)
        except Exception:
            log.exception("GET %s failed", self.path)
            self._json({"error": "Internal error, see the server log"}, 500)

    def do_POST(self) -> None:
        try:
            if not self._host_ok() or not self._write_ok():
                return
            body = self._body()
            if body is not None:
                self._post(urlsplit(self.path).path, body)
        except Exception:
            log.exception("POST %s failed", self.path)
            self._json({"error": "Internal error, see the server log"}, 500)

    def _post(self, path: str, body: dict) -> None:
        store = self.server.store
        extra: dict = {}
        try:
            if path == "/api/move":
                order = body.get("order", [])
                if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
                    raise ValueError("order must be a list of ids")
                store.move(_text(body, "id"), _text(body, "lane"), order)
            elif path == "/api/note":
                store.set_note(_text(body, "id"), _text(body, "note", required=False)[:MAX_NOTE])
            elif path == "/api/items":
                title, url = split_title_url(_text(body, "title"))
                store.add_local(title, url, _text(body, "lane", required=False) or "today")
            elif path == "/api/items/delete":
                store.delete_local(_text(body, "id"))
            elif path == "/api/todos":
                store.add_todo(_text(body, "id"), _text(body, "text"))
            elif path == "/api/todos/remove":
                store.remove_todo(_text(body, "todo"))
            elif path == "/api/todos/edit":
                store.edit_todo(_text(body, "todo"), _text(body, "text"))
            elif path == "/api/open":
                item = store.get(_text(body, "id"))
                if not item or not is_http(item["url"]):
                    raise KeyError("no link")
                open_url(item["url"])
                return self._json({"ok": True})
            elif path == "/api/sync":
                extra["report"] = sync.run(self.server.cfg, store)
            elif path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.stop, daemon=True).start()
                return
            else:
                return self._json({"error": "Not found"}, 404)
        except KeyError:
            return self._json({"error": "That card or to-do is no longer on the board"}, 404)
        except (ValueError, OSError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({**board_state(store, self.server.cfg), **extra})

    # --- request checks --------------------------------------------------------

    def _host_ok(self) -> bool:
        if self.headers.get("Host", "") in self.server.allowed_hosts:
            return True
        self._json({"error": "Forbidden host"}, 403)
        return False

    def _write_ok(self) -> bool:
        origin = self.headers.get("Origin")
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        same_origin = origin is None or (
            urlsplit(origin).scheme == "http" and urlsplit(origin).netloc in self.server.allowed_hosts
        )
        if self.headers.get("X-Workhub") == "1" and content_type == "application/json" and same_origin:
            return True
        self._json({"error": "Forbidden"}, 403)
        return False

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._json({"error": "Bad request body"}, 400)
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json({"error": "The body must be a JSON object"}, 400)
            return None
        return body

    # --- responses -------------------------------------------------------------

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _send(self, code: int, payload: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(payload)


def _text(body: dict, key: str, required: bool = True) -> str:
    value = body.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{key} is missing")
    return value.strip()


def serve(cfg: Config) -> None:
    store = Store(cfg.db_path)
    try:
        httpd = BoardServer(cfg, store)
    except OSError as e:
        store.close()
        raise SystemExit(f"workhub: cannot listen on 127.0.0.1:{cfg.board.port} ({e.strerror}). Change [board] port.")

    def on_term(*_):
        threading.Thread(target=httpd.stop, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    threading.Thread(target=httpd.sync_loop, name="sync", daemon=True).start()
    log.info("Board on http://localhost:%s (pid %s)", cfg.board.port, os.getpid())
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping.set()
        httpd.server_close()
        store.close()
        log.info("Board stopped")
