"""Tiny local review dashboard (stdlib only, bound to 127.0.0.1): read, approve, reject, publish."""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import pipeline
from .render import preview_html

STYLE = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--ok:#15803d;--bad:#b91c1c;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--ok:#4ade80;--bad:#f87171;--accent:#93c5fd}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1000px;padding:24px 16px}
a{color:var(--accent)}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}.muted{color:var(--muted)}form{display:inline}
button{font:inherit;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.ok{color:var(--ok)}.bad{color:var(--bad)}iframe{width:100%;height:70vh;border:1px solid var(--line);border-radius:8px}
"""


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>").encode()


def _button(aid: int, action: str, label: str, primary: bool = False) -> str:
    cls = " class='primary'" if primary else ""
    return f"<form method='post' action='/articles/{aid}/{action}'><button{cls}>{label}</button></form>"


def index(ctx: Any) -> bytes:
    rows = []
    for a in ctx.db.articles():
        rows.append(
            f"<tr><td>#{a['id']}</td><td><a href='/articles/{a['id']}'>{html.escape(a['h1'] or a['keyword'])}</a>"
            f"<div class='muted'>{html.escape(a['keyword'])}</div></td><td>{a['score']}</td><td>{a['status']}</td>"
            f"<td>{_actions(a)}</td></tr>")
    body = (f"<h1>{html.escape(ctx.cfg.site.name or ctx.cfg.site.url)}</h1>"
            f"<p class='muted'>Publish target: {ctx.cfg.publish.target} · review {'on' if ctx.cfg.schedule.review else 'off'}</p>"
            "<table><tr><th></th><th>Article</th><th>Score</th><th>Status</th><th></th></tr>" + "".join(rows) + "</table>")
    return _page("Autopilot review", body)


def _actions(a: dict[str, Any]) -> str:
    if a["status"] in ("pending_review", "approved"):
        return _button(a["id"], "publish", "Publish", True) + " " + _button(a["id"], "reject", "Reject")
    if a["remote_url"]:
        return f"<a href='{html.escape(a['remote_url'])}'>view</a>"
    return ""


def detail(ctx: Any, aid: int) -> bytes | None:
    a = ctx.db.article(aid)
    if not a:
        return None
    report = json.loads(a["score_report"] or "{}")
    checks = "".join(f"<li class='{'ok' if c['passed'] else 'bad'}'>{'✓' if c['passed'] else '✗'} {html.escape(c['name'])} "
                     f"<span class='muted'>{html.escape(c['detail'])}</span></li>" for c in report.get("checks", []))
    body = (f"<p><a href='/'>← all articles</a></p><h1>{html.escape(a['h1'])}</h1>"
            f"<p><b>SEO title:</b> {html.escape(a['meta_title'])}<br><b>Description:</b> {html.escape(a['meta_description'])}<br>"
            f"<b>Slug:</b> {html.escape(a['slug'])} · <b>Score:</b> {a['score']} · <b>Status:</b> {a['status']}</p>"
            f"<p>{_actions(a)}</p><ul>{checks}</ul><iframe src='/articles/{aid}/preview'></iframe>")
    return _page(a["h1"], body)


def serve(ctx: Any, port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if not parts:
                return self._send(200, index(ctx))
            if parts[0] == "articles" and len(parts) >= 2 and parts[1].isdigit():
                aid = int(parts[1])
                if len(parts) == 3 and parts[2] == "preview" and (a := ctx.db.article(aid)):
                    return self._send(200, preview_html(a, ctx.cfg.site.language).encode())
                if (page := detail(ctx, aid)) is not None:
                    return self._send(200, page)
            self._send(404, _page("Not found", "<p>Not found. <a href='/'>Back</a></p>"))

        def do_POST(self) -> None:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode()) if length else {}
            if len(parts) != 3 or parts[0] != "articles" or not parts[1].isdigit():
                return self._send(404, b"not found", "text/plain")
            aid, action = int(parts[1]), parts[2]
            try:
                if action == "publish":
                    pipeline.publish_article(ctx.cfg, ctx.db, ctx.client, aid, status=(form.get("status") or [None])[0])
                elif action == "reject":
                    ctx.db.update_article(aid, status="rejected")
                else:
                    return self._send(400, b"unknown action", "text/plain")
            except Exception as exc:
                return self._send(500, _page("Error", f"<p class='bad'>{html.escape(str(exc))}</p><a href='/'>Back</a>"))
            self.send_response(303)
            self.send_header("Location", f"/articles/{aid}")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    server = HTTPServer(  # single-threaded: the sqlite connection belongs to this thread
        ("127.0.0.1", port), Handler)
    print(f"Review dashboard on http://127.0.0.1:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
