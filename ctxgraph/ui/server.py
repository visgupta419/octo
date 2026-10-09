"""Tiny HTTP server for the UI: static files plus the JSON API, stdlib only."""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..store.db import Database
from .api import Api

STATIC_DIR = Path(__file__).with_name("static")


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ctxgraph-ui"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        # -- helpers ---------------------------------------------------------
        def _json(self, payload, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, name: str) -> None:
            path = (STATIC_DIR / name).resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.is_file():
                self._json({"error": "not found"}, 404)
                return
            data = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _param(self, qs: dict, key: str, default=None):
            vals = qs.get(key)
            return vals[0] if vals else default

        # -- routes ----------------------------------------------------------
        def do_GET(self) -> None:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            route = url.path
            try:
                if route in ("/", "/index.html"):
                    return self._static("index.html")
                if route == "/favicon.ico":
                    return self._static("favicon.svg")
                if route.startswith("/static/"):
                    return self._static(route[len("/static/"):])
                with api.lock:
                    if route == "/api/overview":
                        return self._json(api.overview())
                    if route == "/api/entities":
                        return self._json(api.entities(self._param(qs, "type"), self._param(qs, "q"), int(self._param(qs, "limit", 100)), int(self._param(qs, "offset", 0))))
                    if route == "/api/entity":
                        d = api.entity(self._param(qs, "id", ""))
                        return self._json(d if d else {"error": "not found"}, 200 if d else 404)
                    if route == "/api/neighborhood":
                        return self._json(api.neighborhood(self._param(qs, "id", ""), int(self._param(qs, "depth", 2)), int(self._param(qs, "max", 40)), int(self._param(qs, "first", 18)), int(self._param(qs, "second", 4))))
                    if route == "/api/files":
                        return self._json(api.files(self._param(qs, "q"), int(self._param(qs, "limit", 200)), int(self._param(qs, "offset", 0))))
                    if route == "/api/file":
                        d = api.file(self._param(qs, "path", ""))
                        return self._json(d if d else {"error": "not found"}, 200 if d else 404)
                    if route == "/api/chunk":
                        d = api.chunk(self._param(qs, "id", ""))
                        return self._json(d if d else {"error": "not found"}, 200 if d else 404)
                    if route == "/api/log":
                        return self._json(api.query_log(int(self._param(qs, "limit", 50))))
                    if route == "/api/search":
                        return self._json(api.search_chunks(self._param(qs, "q", ""), int(self._param(qs, "limit", 30))))
                return self._json({"error": "not found"}, 404)
            except Exception as exc:  # surface to the page rather than dying
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def do_POST(self) -> None:
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid json"}, 400)
            try:
                if url.path == "/api/query":
                    with api.lock:
                        return self._json(api.query(body))
                return self._json({"error": "not found"}, 404)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


class UiServer:
    def __init__(self, cfg: Config, host: str = "127.0.0.1", port: int = 7337):
        self.db = Database(cfg.db_path, check_same_thread=False)
        self.api = Api(cfg, self.db)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.api))
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def serve_forever(self) -> None:
        try:
            self.httpd.serve_forever()
        finally:
            self.db.close()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.db.close()


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 7337, open_browser: bool = True) -> None:
    server = UiServer(cfg, host, port)
    print(f"ctxgraph ui: {server.url}  (index {cfg.db_path}, Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(server.url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
