from __future__ import annotations

import html
import json
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aizim.domain.serialization import JsonValue

from .report import snapshot

_PAGE = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


def document(data: dict[str, JsonValue] | None = None) -> str:
    encoded = (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return _PAGE.replace("__SNAPSHOT__", encoded)


def serve(project: Path, *, port: int = 8765, prices: dict[str, JsonValue] | None = None) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host = self.headers.get("Host", "").split(":", 1)[0]
            if host not in {"127.0.0.1", "localhost"}:
                self.send_error(403)
                return
            try:
                if self.path == "/":
                    body, content_type = document().encode(), "text/html; charset=utf-8"
                elif self.path == "/api/snapshot":
                    body = json.dumps(snapshot(project, prices), ensure_ascii=False).encode()
                    content_type = "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
            except Exception:
                self.send_error(503, "Research state temporarily unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Aizim dashboard: http://127.0.0.1:{server.server_port}", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()


def exposition(data: dict[str, JsonValue]) -> str:
    """A portable human-facing view, independent of any hosted publishing service."""
    return document(data).replace(
        "<title>Aizim Research</title>",
        f"<title>{html.escape(str(data['project']))} — Research record</title>",
    )
