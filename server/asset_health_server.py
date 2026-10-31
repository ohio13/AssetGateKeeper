#!/usr/bin/env python3
r"""AssetHealth Server — the receiving end of the AssetGatekeeper ecosystem.

A single stdlib-only file: ``http.server`` + ``sqlite3`` + ``json``. No Flask, no
ORM, no JS build step. Drop it next to the gatekeeper, run it, open the dashboard.

    python server/asset_health_server.py --port 8080
    # then point the gate at it:
    #   asset_gatekeeper.py D:\Exports --telemetry-url http://127.0.0.1:8080/api/telemetry

Routes
    POST /api/telemetry   one verdict per request, JSON body (the gatekeeper's payload)
    GET  /dashboard       HTML: totals, pass rate, wall of shame, top bottlenecks
    GET  /api/summary     the same numbers as JSON, for a wallboard or a test
    GET  /               302 -> /dashboard

Auth
    Set ``AUTH_TOKEN`` (or ``--token``) and every request must carry
    ``Authorization: Bearer <token>``. Unset means "trust the network", which is
    fine for 127.0.0.1 and not fine for anything else — the server says so on boot.

Exit codes: 0 normal shutdown, 2 startup/config problem.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import html
import json
import os
import re
import socketserver
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_HOST = "127.0.0.1"       # loopback by default: no accidental exposure
DEFAULT_PORT = 8080
DEFAULT_DB = "asset_health.db"
DEFAULT_TOKEN_ENV = "AUTH_TOKEN"

MAX_BODY_BYTES = 64 * 1024       # one verdict is ~1 KB; 64 KB is already generous
WALL_OF_SHAME_LIMIT = 20
BOTTLENECK_LIMIT = 8

UTC_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    received_at TEXT    NOT NULL,
    hostname    TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',
    os          TEXT    NOT NULL DEFAULT '',
    filename    TEXT    NOT NULL,
    filepath    TEXT    NOT NULL DEFAULT '',
    filesize    INTEGER NOT NULL DEFAULT -1,
    passed      INTEGER NOT NULL,              -- 0/1, SQLite has no bool
    metric      TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS problems (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdicts(id) ON DELETE CASCADE,
    message    TEXT    NOT NULL
);

-- The dashboard's three queries, and nothing else.
CREATE INDEX IF NOT EXISTS idx_verdicts_passed    ON verdicts(passed);
CREATE INDEX IF NOT EXISTS idx_verdicts_timestamp ON verdicts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_problems_verdict   ON problems(verdict_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# One writer at a time keeps WAL happy without a storm of busy retries.
_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def open_db(db_path: str | Path):
    """One short-lived connection per unit of work, always closed.

    SQLite connections are cheap; leaking them in a server that runs for weeks is
    not. ``sqlite3.Connection`` used as a context manager commits but never
    closes, which is exactly the trap this wrapper exists to avoid.
    """
    connection = sqlite3.connect(str(db_path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(db_path: str | Path) -> None:
    """Create the schema on first run. Safe to call on every boot."""
    with open_db(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")  # concurrent readers
        connection.executescript(SCHEMA)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise(payload: dict) -> dict:
    """Map a gatekeeper payload (or a friendly variant) onto the columns.

    Accepts both the tool's own field names and the shorter ones a hand-written
    curl might use, because the first person to POST here will be someone with
    curl and a guess.
    """
    def pick(*keys, default=None):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return default

    passed = pick("passed", default=None)
    if not isinstance(passed, bool):
        raise ValueError("'passed' must be true or false")

    problems = pick("problems", default=[])
    if problems is None:
        problems = []
    if not isinstance(problems, list) or not all(isinstance(item, str) for item in problems):
        raise ValueError("'problems' must be a list of strings")

    filename = pick("name", "filename", "file")
    if not isinstance(filename, str) or not filename:
        raise ValueError("'name' (filename) is required")

    filesize = pick("size_bytes", "filesize", default=-1)
    if isinstance(filesize, bool) or not isinstance(filesize, int):
        filesize = -1

    return {
        "timestamp": str(pick("timestamp", default=_utc_now())),
        "received_at": _utc_now(),
        "hostname": str(pick("hostname", default="")),
        "username": str(pick("username", default="")),
        "os": str(pick("platform", "os", default="")),
        "filename": filename,
        "filepath": str(pick("path", "filepath", default=filename)),
        "filesize": filesize,
        "passed": 1 if passed else 0,
        "metric": str(pick("metric", default="")),
        "problems": problems,
    }


def store_verdict(db_path: str | Path, payload: dict) -> int:
    """Insert one verdict plus its problems atomically. Returns the new row id."""
    row = _normalise(payload)
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO verdicts (timestamp, received_at, hostname, username, os,"
            " filename, filepath, filesize, passed, metric)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["timestamp"], row["received_at"], row["hostname"], row["username"],
             row["os"], row["filename"], row["filepath"], row["filesize"],
             row["passed"], row["metric"]),
        )
        verdict_id = int(cursor.lastrowid or 0)
        connection.executemany(
            "INSERT INTO problems (verdict_id, message) VALUES (?, ?)",
            [(verdict_id, message) for message in row["problems"]],
        )
    return verdict_id


# --------------------------------------------------------------------------- #
# Dashboard queries
# --------------------------------------------------------------------------- #

def totals(db_path: str | Path) -> dict:
    """Global status: how much was gated, and how much of it survived."""
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total,"
            "       COALESCE(SUM(passed), 0) AS passed"
            " FROM verdicts"
        ).fetchone()
        hosts = connection.execute(
            "SELECT COUNT(DISTINCT hostname) FROM verdicts WHERE hostname <> ''"
        ).fetchone()[0]
    total = int(row["total"])
    passed = int(row["passed"])
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed * 100.0 / total, 1) if total else 0.0,
        "hosts": int(hosts or 0),
    }


def wall_of_shame(db_path: str | Path, limit: int = WALL_OF_SHAME_LIMIT) -> list[dict]:
    """The last N failures, each with every reason it broke."""
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT v.id, v.timestamp, v.username, v.hostname, v.filename, v.filepath,"
            "       v.metric,"
            # char(10) is a real newline; a '\n' literal in SQL is a backslash and an n.
            "       (SELECT GROUP_CONCAT(p.message, char(10))"
            "          FROM problems p WHERE p.verdict_id = v.id) AS reasons"
            " FROM verdicts v"
            " WHERE v.passed = 0"
            " ORDER BY v.timestamp DESC, v.id DESC"
            " LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "username": row["username"],
            "hostname": row["hostname"],
            "filename": row["filename"],
            "metric": row["metric"],
            "reasons": [line for line in (row["reasons"] or "").split("\n") if line],
        }
        for row in rows
    ]


# Problem text -> the rule a technical lead actually thinks in. Ordered: the first
# pattern that matches wins, so put the specific ones above the generic ones.
_RULES: tuple[tuple[str, str], ...] = (
    # Specific before generic: "mesh contains no triangles (empty or corrupt export)"
    # must not be counted as a triangle-budget failure.
    (r"empty or corrupt|contains no triangles", "Empty export"),
    (r"triangles exceeds", "Triangle budget"),
    (r"material slots", "Material slots"),
    (r"power of two", "Non-power-of-two"),
    (r"standard suffix", "Texture naming"),
    (r"maximum resolution|exceeds the .* maximum", "Texture resolution"),
    (r"bone weights", "Bone influences"),
    (r"UV channels", "UV channels"),
    (r"degenerate", "Degenerate triangles"),
    (r"extension but is actually", "Mislabeled format"),
    (r"could not read|unreadable", "Unreadable file"),
)
_RULES_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), label) for pattern, label in _RULES)


def classify(problem: str) -> str:
    """Bucket one problem string. Unknown text is kept visible as 'Other'."""
    for pattern, label in _RULES_COMPILED:
        if pattern.search(problem):
            return label
    return "Other"


def bottlenecks(db_path: str | Path, limit: int = BOTTLENECK_LIMIT) -> list[tuple[str, int]]:
    """Which rule is broken most often — the ranking that decides next sprint."""
    with open_db(db_path) as connection:
        rows = connection.execute("SELECT message FROM problems").fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        label = classify(row["message"])
        counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

CSS = """
:root {
  --bg: #0f1218; --panel: #171c25; --line: #232b38; --text: #e6e9ef;
  --muted: #8b94a7; --good: #3ecf8e; --bad: #ff6b6b; --accent: #7aa2f7;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--text);
  font: 15px/1.55 ui-sans-serif, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; }
header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 32px 0 10px; font-weight: 600; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; margin-top: 22px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
.card .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.07em; }
.card .v { font-size: 30px; font-weight: 600; margin-top: 6px; font-variant-numeric: tabular-nums; }
.v.good { color: var(--good); } .v.bad { color: var(--bad); } .v.accent { color: var(--accent); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
     color: var(--muted); padding: 10px 14px; border-bottom: 1px solid var(--line); font-weight: 600; }
td { padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.mono, .mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 13px; }
td.name { font-weight: 600; }
.why { color: var(--bad); font-size: 13px; }
.why div + div { margin-top: 3px; }
.who { color: var(--muted); font-size: 12px; }
.empty { padding: 22px 16px; color: var(--good); }
.bars { padding: 16px 18px; display: grid; gap: 10px; }
.bar { display: grid; grid-template-columns: 170px 1fr 46px; gap: 12px; align-items: center; }
.bar .label { font-size: 13px; color: var(--text); }
.bar .track { background: #0c1017; border: 1px solid var(--line); border-radius: 999px; height: 9px; }
.bar .fill { background: linear-gradient(90deg, var(--accent), #bb9af7); height: 100%; border-radius: 999px; }
.bar .n { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); font-size: 13px; }
footer { color: var(--muted); font-size: 12px; margin-top: 36px; display: flex; gap: 16px; flex-wrap: wrap; }
code { background: #0c1017; border: 1px solid var(--line); border-radius: 5px; padding: 1px 6px;
       font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


def _e(value: object) -> str:
    """Every string on this page is artist-supplied; escape all of it."""
    return html.escape("" if value is None else str(value))


def render_dashboard(db_path: str | Path) -> str:
    stats = totals(db_path)
    shame = wall_of_shame(db_path)
    tops = bottlenecks(db_path)
    generated = _utc_now()

    rate = stats["pass_rate"]
    rate_class = "good" if rate >= 90 else ("accent" if rate >= 70 else "bad")

    if shame:
        shame_rows = []
        for entry in shame:
            reasons = "".join(f"<div>{_e(reason)}</div>" for reason in entry["reasons"]) or "<div>no reason recorded</div>"
            who = _e(entry["username"] or "unknown")
            host = f" <span class=\"mono\">({_e(entry['hostname'])})</span>" if entry["hostname"] else ""
            shame_rows.append(
                "<tr>"
                f"<td class=\"who mono\">{_e(entry['timestamp'][:19].replace('T', ' '))}</td>"
                f"<td class=\"name\">{_e(entry['filename'])}<div class=\"who\">{who}{host}</div></td>"
                f"<td class=\"why\">{reasons}</td>"
                "</tr>"
            )
        shame_html = (
            "<div class=\"panel\"><table><thead><tr>"
            "<th style=\"width:150px\">When</th><th style=\"width:290px\">Asset</th>"
            "<th>Why it failed</th></tr></thead><tbody>"
            + "".join(shame_rows)
            + "</tbody></table></div>"
        )
    else:
        shame_html = "<div class=\"panel\"><div class=\"empty\">Nothing has failed yet. Enjoy it.</div></div>"

    if tops:
        peak = max(count for _, count in tops) or 1
        bars = "".join(
            f"<div class=\"bar\"><div class=\"label\">{_e(label)}</div>"
            f"<div class=\"track\"><div class=\"fill\" style=\"width:{max(4, round(count * 100 / peak))}%\"></div></div>"
            f"<div class=\"n\">{count}</div></div>"
            for label, count in tops
        )
        tops_html = f"<div class=\"panel\"><div class=\"bars\">{bars}</div></div>"
    else:
        tops_html = "<div class=\"panel\"><div class=\"empty\">No violations recorded.</div></div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AssetHealth — studio pipeline dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AssetHealth</h1>
    <span class="sub">studio pipeline dashboard</span>
  </header>
  <div class="sub">Every number below comes from live verdicts posted by AssetGatekeeper.</div>

  <h2>Global status</h2>
  <div class="cards">
    <div class="card"><div class="k">Assets scanned</div><div class="v accent">{stats['total']:,}</div></div>
    <div class="card"><div class="k">Passed</div><div class="v good">{stats['passed']:,}</div></div>
    <div class="card"><div class="k">Failed</div><div class="v bad">{stats['failed']:,}</div></div>
    <div class="card"><div class="k">Pass rate</div><div class="v {rate_class}">{rate}%</div></div>
    <div class="card"><div class="k">Workstations</div><div class="v">{stats['hosts']:,}</div></div>
  </div>

  <h2>Wall of shame — last {min(len(shame), WALL_OF_SHAME_LIMIT)} failures</h2>
  {shame_html}

  <h2>Top bottlenecks</h2>
  {tops_html}

  <footer>
    <span>Generated {_e(generated)}</span>
    <span>Database <code>{_e(db_path)}</code></span>
    <span><code>POST /api/telemetry</code></span>
    <span><a href="/api/summary" style="color:#7aa2f7">/api/summary</a></span>
  </footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    """Three routes, one auth check, no framework."""

    server_version = "AssetHealth/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"   # keep-alive; Content-Length is always set below

    # -- plumbing ----------------------------------------------------------- #

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _authorised(self) -> bool:
        """Bearer check, when a token is configured. Constant-time compare."""
        expected = self.server.auth_token  # type: ignore[attr-defined]
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return False
        return _constant_time_equal(presented.strip(), expected)

    # -- routes ------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send(302, b"", "text/plain; charset=utf-8", {"Location": "/dashboard"})
            elif path == "/dashboard":
                if not self._authorised():
                    self._unauthorised()
                    return
                body = render_dashboard(self.server.db_path).encode("utf-8")  # type: ignore[attr-defined]
                self._send(200, body, "text/html; charset=utf-8")
            elif path == "/api/summary":
                if not self._authorised():
                    self._unauthorised()
                    return
                db_path = self.server.db_path  # type: ignore[attr-defined]
                self._json(200, {
                    "totals": totals(db_path),
                    "bottlenecks": [{"rule": label, "count": count}
                                    for label, count in bottlenecks(db_path)],
                })
            else:
                self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # a query error must not kill the handler thread
            self._json(500, {"error": "internal error", "detail": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/telemetry":
            self._json(404, {"error": "not found", "path": path})
            return
        if not self._authorised():
            self._unauthorised()
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._json(411, {"error": "Content-Length required"})
            return
        try:
            length = int(length_header)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": f"body must be between 0 and {MAX_BODY_BYTES} bytes"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": "body is not valid JSON"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return

        try:
            verdict_id = store_verdict(self.server.db_path, payload)  # type: ignore[attr-defined]
        except ValueError as exc:   # shape problem: the client should fix it
            self._json(400, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._json(500, {"error": "database error", "detail": str(exc)})
            return
        self._json(201, {"stored": True, "id": verdict_id})

    def _unauthorised(self) -> None:
        self._json(401, {"error": "unauthorised: send 'Authorization: Bearer <token>'"})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {"error": "method not allowed"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def _constant_time_equal(left: str, right: str) -> bool:
    """Compare tokens without leaking length/prefix timing (and without a dep)."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class AssetHealthServer(socketserver.ThreadingTCPServer):
    """One thread per connection, so a slow browser tab cannot stall ingest.

    ``daemon_threads`` matters more than it looks: without it a keep-alive
    connection from an idle dashboard tab would block Ctrl+C for 5 minutes.
    """

    allow_reuse_address = True     # restart immediately after a crash
    daemon_threads = True
    request_queue_size = 64        # a studio's worth of retries in the backlog

    def __init__(self, address: tuple[str, int], db_path: str | Path, token: str = "",
                 quiet: bool = False):
        self.db_path = str(db_path)
        self.auth_token = token
        self.quiet = quiet
        super().__init__(address, Handler)


def build_server(host: str, port: int, db_path: str | Path,
                 token: str = "", quiet: bool = False) -> AssetHealthServer:
    return AssetHealthServer((host, port), db_path, token, quiet)


# --------------------------------------------------------------------------- #
# Seed data — so the dashboard is not an empty page on first run
# --------------------------------------------------------------------------- #

_SEED: tuple[tuple[str, str, str, str, str, int, int, str, list[str]], ...] = (
    # host, user, os, filename, path, size, passed, metric, problems
    ("WS-ART-014", "jdoe", "Windows-11", "boss_statue.gltf", "creatures/boss_statue.gltf",
     1_160_991, 0, "72,500 tris / 4 mat",
     ["72,500 triangles exceeds the 50,000 budget (+22,500)",
      "4 material slots exceeds the 3 slot budget (draw calls)"]),
    ("WS-ART-022", "msato", "Windows-11", "hull_Albedo.png", "ships/hull_Albedo.png",
     8_388_608, 0, "4096x1024", ["4096x1024 exceeds the 2048x2048 maximum resolution"]),
    ("WS-ART-014", "jdoe", "Windows-11", "ui_icon.png", "ui/ui_icon.png",
     40_960, 0, "300x300",
     ["300x300 is not a power of two",
      "name does not end with a standard suffix (_Albedo, _Normal, _Roughness)"]),
    ("WS-TD-003", "arivera", "Windows-11", "rig_heavy.gltf", "characters/rig_heavy.gltf",
     290_400, 0, "18,004 tris / 2 mat / 6 inf",
     ["6 bone weights on one vertex exceeds the 4 influence limit"]),
    ("WS-ART-031", "kli", "Windows-11", "uv_heavy.gltf", "props/uv_heavy.gltf",
     120_000, 0, "1,200 tris / 5 uv",
     ["5 UV channels exceeds the 4 channel limit"]),
    ("WS-ART-022", "msato", "Windows-11", "broken_glass.obj", "props/broken_glass.obj",
     3_200, 0, "12 tris / 1 mat",
     ["1 degenerate triangles (zero-area) - Nanite and ray tracing compute garbage from these"]),
    ("WS-ART-031", "kli", "Windows-11", "tokyo_sign.gltf", "env/tokyo_sign.gltf",
     400_000, 0, "120,400 tris / 2 mat",
     ["120,400 triangles exceeds the 50,000 budget (+70,400)"]),
    ("WS-ART-014", "jdoe", "Windows-11", "crate_stack.gltf", "props/crate_stack.gltf",
     210_000, 0, "58,000 tris / 3 mat",
     ["58,000 triangles exceeds the 50,000 budget (+8,000)"]),
    ("WS-ART-022", "msato", "Windows-11", "rock_Albedo.png", "env/rock_Albedo.png",
     2_097_152, 0, "1000x1000",
     ["1000x1000 is not a power of two"]),
    ("WS-TD-003", "arivera", "Windows-11", "door_frame.gltf", "props/door_frame.gltf",
     9_400, 1, "900 tris / 1 mat", []),
    ("WS-ART-014", "jdoe", "Windows-11", "prop_crate.glb", "props/prop_crate.glb",
     88_120, 1, "1,800 tris / 2 mat", []),
    ("WS-TD-003", "arivera", "Windows-11", "lantern.gltf", "props/lantern.gltf",
     12_004, 1, "1,200 tris / 1 mat", []),
    ("WS-ART-031", "kli", "Windows-11", "bark_Albedo.png", "nature/bark_Albedo.png",
     262_144, 1, "1024x1024", []),
)


def seed_if_first_launch(db_path: str | Path) -> bool:
    """Insert demo rows exactly once, ever. Returns True if this was that once."""
    with open_db(db_path) as connection:
        marker = connection.execute(
            "SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        if marker is not None:
            return False
        existing = connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
        if existing:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")
            return False

    base = time.time() - 3 * 3600
    for index, (host, user, os_name, filename, path, size, passed, metric, problems) in enumerate(_SEED):
        stamp = datetime.fromtimestamp(base + index * 1200, timezone.utc).isoformat(timespec="seconds")
        store_verdict(db_path, {
            "timestamp": stamp, "hostname": host, "username": user, "platform": os_name,
            "name": filename, "path": path, "size_bytes": size, "passed": bool(passed),
            "metric": metric, "problems": problems,
        })
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset_health_server",
        description="Studio dashboard for AssetGatekeeper verdicts (stdlib only).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to bind (default {DEFAULT_HOST}; 0.0.0.0 exposes it)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--db", default=os.environ.get("ASSET_HEALTH_DB", DEFAULT_DB),
                        help=f"SQLite file (default {DEFAULT_DB}, or $ASSET_HEALTH_DB)")
    parser.add_argument("--token", default=os.environ.get(DEFAULT_TOKEN_ENV, ""),
                        help=f"require 'Authorization: Bearer <token>' (default ${DEFAULT_TOKEN_ENV})")
    parser.add_argument("--no-seed", action="store_true",
                        help="do not insert the demo verdicts on a brand-new database")
    parser.add_argument("--quiet", action="store_true", help="only log errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser()
    if db_path.parent and not db_path.parent.exists():
        print(f"error: database folder does not exist: {db_path.parent}", file=sys.stderr)
        return 2
    try:
        init_db(db_path)
    except sqlite3.Error as exc:
        print(f"error: cannot initialise {db_path}: {exc}", file=sys.stderr)
        return 2

    seeded = False
    if not args.no_seed:
        try:
            seeded = seed_if_first_launch(db_path)
        except sqlite3.Error as exc:
            print(f"warning: seeding skipped ({exc})", file=sys.stderr)

    try:
        httpd = build_server(args.host, args.port, db_path, args.token, args.quiet)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2

    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"AssetHealth listening on http://{shown}:{port}/dashboard")
    print(f"  database   {db_path.resolve()}")
    print("  ingest     POST /api/telemetry")
    if args.token:
        print("  auth       Bearer token required")
    else:
        print("  auth       DISABLED - fine on loopback, not on a shared network")
    if seeded:
        print(f"  seeded     {len(_SEED)} demo verdicts (first launch)")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
