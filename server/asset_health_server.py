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
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
    metric      TEXT    NOT NULL DEFAULT '',
    -- PASSED / HEALED / FAILED. A healed asset passed, but the studio still
    -- wants a record that the file was rewritten on its way in.
    status      TEXT    NOT NULL DEFAULT 'PASSED',
    -- Tenant. NULL means "before tenancy existed" (or single-tenant mode).
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS problems (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdicts(id) ON DELETE CASCADE,
    message    TEXT    NOT NULL
);

-- What the (opt-in) healer changed. Empty for everything else.
CREATE TABLE IF NOT EXISTS modifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdicts(id) ON DELETE CASCADE,
    action     TEXT    NOT NULL
);

-- The dashboard's three queries, and nothing else.
CREATE INDEX IF NOT EXISTS idx_verdicts_passed    ON verdicts(passed);
CREATE INDEX IF NOT EXISTS idx_verdicts_timestamp ON verdicts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_problems_verdict   ON problems(verdict_id);
CREATE INDEX IF NOT EXISTS idx_mods_verdict       ON modifications(verdict_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Multi-tenancy. One row per game project or outsource vendor: they share a
-- server, never a row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    -- sha256:<hex> for anything created by --add-project. Plain text is still
    -- accepted so a hand-written row keeps working, and so a studio can rotate
    -- to hashing without a migration script.
    api_token  TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL
);

-- The published budget for a project. This is what /api/rules serves to
-- `--remote-rules`, and what the alerting threshold is measured against.
CREATE TABLE IF NOT EXISTS budgets (
    project_id         INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    max_triangles      INTEGER NOT NULL,
    max_material_slots INTEGER NOT NULL,
    max_texture_edge   INTEGER NOT NULL,
    power_of_two       INTEGER NOT NULL,
    naming             INTEGER NOT NULL,
    auto_heal          INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT    NOT NULL,
    updated_by         TEXT    NOT NULL DEFAULT ''
);

-- Alert throttling. One row per scope ("project:3", or "shared" in legacy
-- mode) holding the epoch of the last message we actually sent. Kept out of
-- `projects` so single-tenant installs get the throttle too.
CREATE TABLE IF NOT EXISTS alerts_state (
    scope_key     TEXT PRIMARY KEY,
    last_alert_at REAL NOT NULL
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
    """Create the schema on first run, and migrate a pre-existing database.

    ``CREATE TABLE IF NOT EXISTS`` silently does nothing to a table that already
    exists, so a database written before healing shipped would never gain the
    ``status`` column. ALTER it explicitly instead of asking people to delete
    their metrics.
    """
    with open_db(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")  # concurrent readers
        connection.executescript(SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(verdicts)").fetchall()}
        if "status" not in columns:
            connection.execute(
                "ALTER TABLE verdicts ADD COLUMN status TEXT NOT NULL DEFAULT 'PASSED'")
            print("asset_health: migrated verdicts, added the status column")
        if "project_id" not in columns:
            # Nullable on purpose: rows that predate tenancy stay unassigned
            # rather than being silently attributed to project 1.
            connection.execute("ALTER TABLE verdicts ADD COLUMN project_id INTEGER "
                               "REFERENCES projects(id) ON DELETE SET NULL")
            print("asset_health: migrated verdicts, added the project_id column")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_verdicts_project ON verdicts(project_id, timestamp DESC)")


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

    modifications = pick("modifications", default=[])
    if modifications is None:
        modifications = []
    if not isinstance(modifications, list) or not all(isinstance(m, str) for m in modifications):
        raise ValueError("'modifications' must be a list of strings")

    # Trust an explicit status when it is one we know; otherwise derive it, so a
    # hand-written curl still produces a coherent row.
    status = pick("status", default=None)
    if status not in ("PASSED", "HEALED", "FAILED"):
        status = "HEALED" if (passed and modifications) else ("PASSED" if passed else "FAILED")

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
        "status": status,
        "problems": problems,
        "modifications": modifications,
    }


def store_verdict(db_path: str | Path, payload: dict,
                  project_id: int | None = None) -> int:
    """Insert one verdict plus its problems atomically. Returns the new row id."""
    row = _normalise(payload)
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO verdicts (timestamp, received_at, hostname, username, os,"
            " filename, filepath, filesize, passed, metric, status, project_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["timestamp"], row["received_at"], row["hostname"], row["username"],
             row["os"], row["filename"], row["filepath"], row["filesize"],
             row["passed"], row["metric"], row["status"], project_id),
        )
        verdict_id = int(cursor.lastrowid or 0)
        connection.executemany(
            "INSERT INTO problems (verdict_id, message) VALUES (?, ?)",
            [(verdict_id, message) for message in row["problems"]],
        )
        connection.executemany(
            "INSERT INTO modifications (verdict_id, action) VALUES (?, ?)",
            [(verdict_id, action) for action in row["modifications"]],
        )
    return verdict_id


# --------------------------------------------------------------------------- #
# Tenancy: projects, tokens, published budgets
# --------------------------------------------------------------------------- #

def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_project(db_path: str | Path, name: str, token: str | None = None) -> tuple[int, str]:
    """Add a project. Returns (id, token) — the token is shown once and hashed.

    Only the hash is stored: a metrics database that leaks is bad, a metrics
    database that also leaks every vendor's token is a breach.
    """
    if not name or not name.strip():
        raise ValueError("project name must not be empty")
    token = token or secrets.token_urlsafe(24)
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO projects (name, api_token, created_at) VALUES (?, ?, ?)",
            (name.strip(), hash_token(token), _utc_now()),
        )
        project_id = int(cursor.lastrowid or 0)
    return project_id, token


def list_projects(db_path: str | Path) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT id, name, created_at FROM projects ORDER BY id").fetchall()
    return [{"id": row["id"], "name": row["name"], "created_at": row["created_at"]} for row in rows]


def _token_matches(presented: str, stored: str) -> bool:
    """Constant-time compare, accepting either a sha256: hash or plain text."""
    if stored.startswith("sha256:"):
        return hmac.compare_digest(hash_token(presented), stored)
    return hmac.compare_digest(presented.encode("utf-8"), stored.encode("utf-8"))


def project_for_token(db_path: str | Path, token: str) -> dict | None:
    """Resolve a bearer token to a project, or None. Never raises on a bad token."""
    if not token:
        return None
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT id, name, api_token FROM projects").fetchall()
    for row in rows:
        if _token_matches(token, row["api_token"]):
            return {"id": row["id"], "name": row["name"]}
    return None


def project_count(db_path: str | Path) -> int:
    with open_db(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0])


# --- published budgets ----------------------------------------------------- #

DEFAULT_BUDGET_ROW = {
    "max_triangles": 50_000,
    "max_material_slots": 3,
    "max_texture_edge": 2_048,
    "power_of_two": True,
    "naming": True,
    "auto_heal": False,
}


def budget_for_project(db_path: str | Path, project_id: int | None) -> dict:
    """The published budget, or the shipped defaults when nothing was published."""
    if project_id is None:
        return dict(DEFAULT_BUDGET_ROW)
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM budgets WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        return dict(DEFAULT_BUDGET_ROW)
    return {
        "max_triangles": int(row["max_triangles"]),
        "max_material_slots": int(row["max_material_slots"]),
        "max_texture_edge": int(row["max_texture_edge"]),
        "power_of_two": bool(row["power_of_two"]),
        "naming": bool(row["naming"]),
        "auto_heal": bool(row["auto_heal"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def budget_document(budget: dict) -> dict:
    """The saved budget as a rules document — exactly what `--remote-rules` eats.

    Same shape as rules.toml, so a client never has to learn a second format and
    the gate's own parser validates it on the way in.
    """
    return {
        "mesh": {
            "max_triangles": int(budget["max_triangles"]),
            "max_material_slots": int(budget["max_material_slots"]),
        },
        "texture": {
            "max_edge": int(budget["max_texture_edge"]),
            "power_of_two": bool(budget["power_of_two"]),
            "naming": bool(budget["naming"]),
        },
        "healer": {"enabled": bool(budget.get("auto_heal", False))},
    }


BUDGET_FIELDS = {
    "max_triangles": (int, 0, 10_000_000),
    "max_material_slots": (int, 1, 64),
    "max_texture_edge": (int, 64, 16_384),
    "power_of_two": (bool, None, None),
    "naming": (bool, None, None),
    "auto_heal": (bool, None, None),
}


def validate_budget_form(form: dict) -> dict:
    """Coerce and bound a published budget. Bad input is a 400, never a stored 0."""
    cleaned: dict[str, object] = {}
    for key, (kind, low, high) in BUDGET_FIELDS.items():
        if key not in form:
            continue
        value = form[key]
        if kind is bool:
            if isinstance(value, bool):
                cleaned[key] = value
            elif isinstance(value, str) and value.lower() in ("true", "false", "on", "off"):
                cleaned[key] = value.lower() in ("true", "on")
            elif isinstance(value, int):
                cleaned[key] = bool(value)
            else:
                raise ValueError(f"{key} must be true or false")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError(f"{key} must be a number")
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if not low <= number <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            cleaned[key] = number
    return cleaned


def save_budget(db_path: str | Path, project_id: int, form: dict,
                updated_by: str = "") -> dict:
    """Publish a budget for a project. Partial forms keep the previous values."""
    cleaned = validate_budget_form(form)
    current = budget_for_project(db_path, project_id)
    merged = {**{k: current[k] for k in DEFAULT_BUDGET_ROW}, **cleaned}
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO budgets (project_id, max_triangles, max_material_slots,"
            " max_texture_edge, power_of_two, naming, auto_heal, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id) DO UPDATE SET"
            " max_triangles = excluded.max_triangles,"
            " max_material_slots = excluded.max_material_slots,"
            " max_texture_edge = excluded.max_texture_edge,"
            " power_of_two = excluded.power_of_two,"
            " naming = excluded.naming,"
            " auto_heal = excluded.auto_heal,"
            " updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (project_id, merged["max_triangles"], merged["max_material_slots"],
             merged["max_texture_edge"], int(merged["power_of_two"]), int(merged["naming"]),
             int(merged["auto_heal"]), _utc_now(), updated_by),
        )
    return budget_for_project(db_path, project_id)


# --------------------------------------------------------------------------- #
# Dashboard queries
# --------------------------------------------------------------------------- #

def _project_clause(project_id: int | None) -> tuple[str, tuple]:
    """Shared WHERE fragment. Every tenant-facing query goes through this."""
    if project_id is None:
        return "", ()
    return " WHERE project_id = ?", (project_id,)


def totals(db_path: str | Path, project_id: int | None = None) -> dict:
    """Global status: how much was gated, and how much of it survived."""
    where, params = _project_clause(project_id)
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total,"
            "       COALESCE(SUM(passed), 0) AS passed,"
            "       COALESCE(SUM(CASE WHEN status = 'HEALED' THEN 1 ELSE 0 END), 0) AS healed"
            " FROM verdicts" + where,
            params,
        ).fetchone()
        hosts = connection.execute(
            "SELECT COUNT(DISTINCT hostname) FROM verdicts"
            " WHERE hostname <> ''" + (" AND project_id = ?" if project_id is not None else ""),
            params,
        ).fetchone()[0]
    total = int(row["total"])
    passed = int(row["passed"])
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "healed": int(row["healed"]),
        "pass_rate": round(passed * 100.0 / total, 1) if total else 0.0,
        "hosts": int(hosts or 0),
    }


def wall_of_shame(db_path: str | Path, limit: int = WALL_OF_SHAME_LIMIT,
                  project_id: int | None = None) -> list[dict]:
    """The last N failures, each with every reason it broke."""
    tenant = " AND v.project_id = ?" if project_id is not None else ""
    params: tuple = (project_id, limit) if project_id is not None else (limit,)
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT v.id, v.timestamp, v.username, v.hostname, v.filename, v.filepath,"
            "       v.metric,"
            # char(10) is a real newline; a '\n' literal in SQL is a backslash and an n.
            "       (SELECT GROUP_CONCAT(p.message, char(10))"
            "          FROM problems p WHERE p.verdict_id = v.id) AS reasons"
            " FROM verdicts v"
            " WHERE v.passed = 0" + tenant +
            " ORDER BY v.timestamp DESC, v.id DESC"
            " LIMIT ?",
            params,
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


def bottlenecks(db_path: str | Path, limit: int = BOTTLENECK_LIMIT,
                project_id: int | None = None) -> list[tuple[str, int]]:
    """Which rule is broken most often — the ranking that decides next sprint."""
    with open_db(db_path) as connection:
        if project_id is None:
            rows = connection.execute("SELECT message FROM problems").fetchall()
        else:
            rows = connection.execute(
                "SELECT p.message FROM problems p"
                " JOIN verdicts v ON v.id = p.verdict_id"
                " WHERE v.project_id = ?", (project_id,)).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        label = classify(row["message"])
        counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


# --------------------------------------------------------------------------- #
# Alerting: how badly over budget, and a notifier that cannot slow ingest
# --------------------------------------------------------------------------- #

# Alert when a budget is exceeded by more than this factor (50% over).
ALERT_FACTOR = 1.5

# One webhook message per scope per cooldown window. A nightly sweep that fails
# 300 assets is one incident, not 300 notifications.
DEFAULT_ALERT_COOLDOWN = 300.0

_TRIANGLE_RE = re.compile(r"([\d,]+) triangles exceeds the ([\d,]+) budget")
_SLOTS_RE = re.compile(r"(\d+) material slots exceeds the (\d+) slot budget")
_EDGE_RE = re.compile(r"(\d+)x(\d+) exceeds the (\d+)x\d+ maximum")


def _num(text: str) -> int:
    return int(text.replace(",", ""))


def overshoots(payload: dict, budget: dict) -> list[tuple[str, float, int, int]]:
    """Return [(metric, value, limit, ratio)] for every budget blown by >ALERT_FACTOR.

    Prefers the structured ``stats`` block; falls back to parsing the problem
    strings, because a client that only sends the human-readable reasons must
    still be able to trigger an alert.
    """
    found: list[tuple[str, float, int, int]] = []
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}

    def consider(label: str, value: int, limit: int) -> None:
        if limit > 0 and value > limit and value / limit > ALERT_FACTOR:
            found.append((label, float(value), int(limit), value / limit))

    triangles = stats.get("triangles")
    if isinstance(triangles, int):
        consider("triangles", triangles, int(budget.get("max_triangles", 0)))
    slots = stats.get("material_slots")
    if isinstance(slots, int):
        consider("material slots", slots, int(budget.get("max_material_slots", 0)))
    width, height = stats.get("width"), stats.get("height")
    if isinstance(width, int) and isinstance(height, int):
        consider("texture edge", max(width, height), int(budget.get("max_texture_edge", 0)))

    for problem in payload.get("problems", []) or []:
        if not isinstance(problem, str):
            continue
        if not found:
            match = _TRIANGLE_RE.search(problem)
            if match:
                consider("triangles", _num(match.group(1)), _num(match.group(2)))
        match = _SLOTS_RE.search(problem)
        if match:
            consider("material slots", _num(match.group(1)), _num(match.group(2)))
        match = _EDGE_RE.search(problem)
        if match:
            consider("texture edge", max(_num(match.group(1)), _num(match.group(2))),
                     _num(match.group(3)))
    return found


def alert_text(payload: dict, budget: dict,
               overshoot: list[tuple[str, float, int, int]]) -> str:
    """The exact line a lead sees in Slack at 2am."""
    reasons = "; ".join(p for p in payload.get("problems", []) if isinstance(p, str)) or "unknown"
    details = ", ".join(f"{label} {int(value):,} vs {limit:,} ({ratio:.1f}x)"
                        for label, value, limit, ratio in overshoot)
    return (f"🔴 CRITICAL PIPELINE ALERT: User {payload.get('username') or 'unknown'} on "
            f"{payload.get('hostname') or 'unknown'} failed validation for "
            f"{payload.get('name') or payload.get('filename') or 'unknown'}. "
            f"Reasons: {reasons}. Over budget: {details}")


def webhook_body(provider: str, text: str) -> bytes:
    """Slack, Discord and Teams all want a slightly different envelope."""
    provider = (provider or "slack").lower()
    if provider == "discord":
        body = {"content": text, "username": "AssetGatekeeper"}
    elif provider == "teams":
        body = {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                "summary": "AssetGatekeeper alert", "themeColor": "D13438", "text": text}
    else:  # slack (also the generic "something that takes {\"text\": ...}")
        body = {"text": text}
    return json.dumps(body).encode("utf-8")


def alert_scope(project_id: int | None) -> str:
    """Throttle key. Tenants do not share a cooldown, and legacy mode still gets one."""
    return f"project:{project_id}" if project_id is not None else "shared"


def claim_alert_slot(db_path: str | Path, scope: str, cooldown: float,
                     now: float | None = None) -> bool:
    """Atomically take the alert slot for `scope`. True means "send it".

    The upsert's WHERE clause is the whole mechanism: only one thread can move
    ``last_alert_at`` forward out of the cooldown window, so two workers that
    fail the same batch at the same instant cannot both alert.

    A cooldown of 0 disables throttling and always returns True.
    """
    if cooldown <= 0:
        return True
    moment = time.time() if now is None else now
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO alerts_state (scope_key, last_alert_at) VALUES (?, ?)"
            " ON CONFLICT(scope_key) DO UPDATE SET last_alert_at = excluded.last_alert_at"
            " WHERE alerts_state.last_alert_at <= ?",
            (scope, moment, moment - cooldown),
        )
        return cursor.rowcount == 1


class Notifier:
    """Fire-and-forget webhook POSTs. Same three rules as the gate's telemetry.

    1. Never raise — a dead webhook must not fail an ingest.
    2. Never block — `submit()` enqueues; one daemon thread owns the sockets.
    3. Never grow without bound — a capped queue drops and counts.
    """

    def __init__(self, url: str, provider: str = "slack", timeout: float = 2.0,
                 queue_max: int = 200) -> None:
        self.url = url
        self.provider = provider
        self.timeout = float(timeout)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._queue: "queue.Queue[str | None]" = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._closed = False
        if not url.lower().startswith("https://"):
            print("asset_health: webhook URL is not https; alert text is sent in clear text",
                  file=sys.stderr)

    def submit(self, text: str) -> None:
        if self._closed:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ah-notifier", daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            self.dropped += 1
            print("asset_health: webhook queue full, dropped an alert", file=sys.stderr)

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            self._post(text)

    def _post(self, text: str) -> None:
        request = urllib.request.Request(
            self.url, data=webhook_body(self.provider, text), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "AssetHealth/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
            self.sent += 1
        except Exception as exc:  # silent by contract; the dashboard is the record
            self.failed += 1
            print(f"asset_health: webhook POST failed ({type(exc).__name__}): {exc}",
                  file=sys.stderr)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for the queue to drain without shutting the worker down.

        Used at shutdown and by tests that assert on what was delivered. Never
        called on the ingest path — that path must not wait for a network.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._thread is None:
                return True
            if self._queue.empty():
                time.sleep(0.02)      # let an in-flight POST finish
                if self._queue.empty():
                    return True
            time.sleep(0.02)
        return False

    def close(self, timeout: float | None = None) -> None:
        self.flush(self.timeout if timeout is None else timeout)
        self._closed = True
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(self.timeout if timeout is None else timeout)


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
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
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
nav { display: flex; gap: 18px; margin-top: 14px; font-size: 13px; flex-wrap: wrap;
      align-items: baseline; }
nav a { color: var(--accent); text-decoration: none; font-weight: 600; }
nav a:hover { text-decoration: underline; }
nav .chips { color: var(--muted); font-size: 12px; letter-spacing: 0.02em; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


def _e(value: object) -> str:
    """Every string on this page is artist-supplied; escape all of it."""
    return html.escape("" if value is None else str(value))


PLANNER_CSS = """
.form { display: grid; gap: 22px; margin-top: 22px; }
.field { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
.field label { display: block; font-size: 13px; color: var(--muted); text-transform: uppercase;
              letter-spacing: 0.07em; margin-bottom: 10px; }
.field .row { display: flex; align-items: center; gap: 16px; }
input[type=range] { -webkit-appearance: none; appearance: none; flex: 1; height: 6px;
  background: #0c1017; border: 1px solid var(--line); border-radius: 999px; outline: none; }
input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 18px; height: 18px;
  border-radius: 50%; background: var(--accent); border: 2px solid #0f1218; cursor: pointer; }
input[type=range]::-moz-range-thumb { width: 16px; height: 16px; border-radius: 50%;
  background: var(--accent); border: 2px solid #0f1218; cursor: pointer; }
output { min-width: 96px; text-align: right; font-variant-numeric: tabular-nums;
         font-size: 15px; font-weight: 600; }
select, button { font: inherit; color: var(--text); background: #0c1017;
  border: 1px solid var(--line); border-radius: 8px; padding: 9px 12px; }
.checks { display: flex; gap: 22px; flex-wrap: wrap; align-items: center; }
.check { display: flex; align-items: center; gap: 8px; font-size: 14px; color: var(--text);
         text-transform: none; letter-spacing: 0; margin: 0; }
input[type=checkbox] { width: 17px; height: 17px; accent-color: var(--accent); }
button { background: var(--accent); color: #0b1020; font-weight: 600; cursor: pointer;
         border: 0; padding: 12px 22px; }
button:hover { filter: brightness(1.08); }
button.secondary { background: transparent; color: var(--text); border: 1px solid var(--line); }
.notice { margin-top: 16px; padding: 12px 16px; border-radius: 8px; font-size: 14px;
          border: 1px solid var(--line); display: none; }
.notice.ok { display: block; color: var(--good); border-color: #1d4034; background: #10231c; }
.notice.err { display: block; color: var(--bad); border-color: #4a2020; background: #241314; }
.hint { color: var(--muted); font-size: 12px; margin-top: 8px; }
/* nav is styled by the shared stylesheet (see CSS above) */
"""


MAX_REFRESH_SECONDS = 3600


def render_dashboard(db_path: str | Path, project_id: int | None = None,
                     project_name: str | None = None, warn: str = "",
                     refresh: int = 0) -> str:
    stats = totals(db_path, project_id)
    shame = wall_of_shame(db_path, project_id=project_id)
    tops = bottlenecks(db_path, project_id=project_id)
    budget = budget_for_project(db_path, project_id)
    generated = _utc_now()
    scope = f"project: {_e(project_name)}" if project_name else "all projects"
    planner_href = "/budget-planner" + (f"?project_id={project_id}" if project_id else "")

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

    # Opt-in only: ?refresh=5 turns the dashboard into a wallboard. Off by default
    # so a stale page never looks like a broken pipeline.
    auto_refresh = (f'<meta http-equiv="refresh" content="{refresh}">'
                    if refresh > 0 else "")
    live_note = (f' <span class="chips">&middot; live, every {refresh}s</span>'
                 if refresh > 0 else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{auto_refresh}
<title>AssetHealth — studio pipeline dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AssetHealth</h1>
    <span class="sub">studio pipeline dashboard &middot; {scope}{live_note}</span>
  </header>
  <div class="sub">Every number below comes from live verdicts posted by AssetGatekeeper.</div>
  {f'<div class="notice err" style="display:block">{_e(warn)}</div>' if warn else ''}
  <nav>
    <a href="{planner_href}">Budget planner &rarr;</a>
    <span class="chips">tris &le; {budget['max_triangles']:,} &nbsp;|&nbsp;
      texture &le; {budget['max_texture_edge']}px &nbsp;|&nbsp;
      materials &le; {budget['max_material_slots']} &nbsp;|&nbsp;
      auto-heal {'on' if budget['auto_heal'] else 'off'}</span>
  </nav>

  <h2>Global status</h2>
  <div class="cards">
    <div class="card"><div class="k">Assets scanned</div><div class="v accent">{stats['total']:,}</div></div>
    <div class="card"><div class="k">Passed</div><div class="v good">{stats['passed']:,}</div></div>
    <div class="card"><div class="k">Failed</div><div class="v bad">{stats['failed']:,}</div></div>
    <div class="card"><div class="k">Pass rate</div><div class="v {rate_class}">{rate}%</div></div>
    <div class="card"><div class="k">Healed (opt-in)</div><div class="v">{stats['healed']:,}</div></div>
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


def render_error_page(status: int, headline: str, detail: str) -> str:
    """A refusal that explains itself, without leaking the project list."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>AssetHealth — {status}</title><style>{CSS}</style></head>
<body><div class="wrap">
  <header><h1>AssetHealth</h1><span class="sub">{status}</span></header>
  <div class="panel" style="margin-top:24px"><div class="empty" style="color:var(--bad)">
    <strong>{_e(headline)}</strong>
  </div></div>
  <div class="sub" style="margin-top:16px">{_e(detail)}</div>
</div></body></html>"""


def render_planner(db_path: str | Path, project_id: int, project_name: str,
                   token: str, projects: list[dict]) -> str:
    """The visual budget editor: sliders, a dropdown, checkboxes, one button."""
    budget = budget_for_project(db_path, project_id)
    options = "".join(
        f'<option value="{row["id"]}"{" selected" if row["id"] == project_id else ""}>'
        f'{_e(row["name"])}</option>' for row in projects)
    updated = budget.get("updated_at") or "never"
    updated_by = budget.get("updated_by") or "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AssetHealth — budget planner</title>
<style>{CSS}{PLANNER_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Budget planner</h1>
    <span class="sub">{_e(project_name)}</span>
  </header>
  <div class="sub">Published budgets are served to every client that runs
    <code>--remote-rules</code> on its next run. No TOML, no build step.</div>
  <nav><a href="/dashboard?project_id={project_id}">&larr; Back to the dashboard</a></nav>

  <form class="form" id="budget-form" data-project="{project_id}" data-token="{_e(token)}">
    <div class="field">
      <label for="project">Project</label>
      <select id="project" disabled>{options}</select>
      <div class="hint">Switch project by opening this page with its own link.</div>
    </div>

    <div class="field">
      <label for="max_triangles">Max triangles</label>
      <div class="row">
        <input id="max_triangles" name="max_triangles" type="range"
               min="0" max="200000" step="500" value="{budget['max_triangles']}">
        <output id="max_triangles_out" for="max_triangles">{budget['max_triangles']:,}</output>
      </div>
      <div class="hint">Per mesh file. 50,000 is the shipped default.</div>
    </div>

    <div class="field">
      <label for="max_material_slots">Max material slots</label>
      <div class="row">
        <input id="max_material_slots" name="max_material_slots" type="range"
               min="1" max="16" step="1" value="{budget['max_material_slots']}">
        <output id="max_material_slots_out" for="max_material_slots">{budget['max_material_slots']}</output>
      </div>
      <div class="hint">Draw-call proxy: distinct materials per mesh.</div>
    </div>

    <div class="field">
      <label for="max_texture_edge">Max texture edge</label>
      <div class="row">
        <select id="max_texture_edge" name="max_texture_edge">
          {"".join(f'<option value="{edge}"{" selected" if edge == budget["max_texture_edge"] else ""}>{edge}px</option>' for edge in (512, 1024, 2048, 4096))}
        </select>
      </div>
    </div>

    <div class="field">
      <label>Rules</label>
      <div class="checks">
        <label class="check"><input type="checkbox" id="power_of_two" name="power_of_two"
          {"checked" if budget["power_of_two"] else ""}> Enforce power of two</label>
        <label class="check"><input type="checkbox" id="naming" name="naming"
          {"checked" if budget["naming"] else ""}> Require naming suffixes</label>
        <label class="check"><input type="checkbox" id="auto_heal" name="auto_heal"
          {"checked" if budget["auto_heal"] else ""}> Enable auto-heal</label>
      </div>
      <div class="hint">Auto-heal lets client machines repair textures and zero-area
        faces, and is adopted only when the repaired file verifies.</div>
    </div>

    <div>
      <button type="submit">Publish budgets</button>
      <div class="hint">Last published {_e(updated)} by {_e(updated_by)}</div>
    </div>
    <div class="notice" id="notice"></div>
  </form>
</div>
<script>
(function () {{
  var form = document.getElementById("budget-form");
  var notice = document.getElementById("notice");

  function sync(input) {{
    var out = document.getElementById(input.id + "_out");
    if (out) out.textContent = Number(input.value).toLocaleString();
  }}
  Array.prototype.forEach.call(form.querySelectorAll('input[type=range]'), function (input) {{
    input.addEventListener("input", function () {{ sync(input); }});
  }});

  form.addEventListener("submit", function (event) {{
    event.preventDefault();
    var payload = {{
      project_id: Number(form.dataset.project),
      max_triangles: Number(document.getElementById("max_triangles").value),
      max_material_slots: Number(document.getElementById("max_material_slots").value),
      max_texture_edge: Number(document.getElementById("max_texture_edge").value),
      power_of_two: document.getElementById("power_of_two").checked,
      naming: document.getElementById("naming").checked,
      auto_heal: document.getElementById("auto_heal").checked
    }};
    notice.className = "notice";
    notice.textContent = "Publishing...";
    fetch("/api/save-budget", {{
      method: "POST",
      headers: {{"Content-Type": "application/json",
                "Authorization": "Bearer " + form.dataset.token}},
      body: JSON.stringify(payload)
    }}).then(function (response) {{
      return response.json().then(function (body) {{ return {{ok: response.ok, body: body}}; }});
    }}).then(function (result) {{
      if (result.ok) {{
        notice.className = "notice ok";
        notice.textContent = "Published. Clients pick this up on their next --remote-rules run.";
      }} else {{
        notice.className = "notice err";
        notice.textContent = "Rejected: " + (result.body.error || "unknown error");
      }}
    }}).catch(function (error) {{
      notice.className = "notice err";
      notice.textContent = "Could not reach the server: " + error;
    }});
  }});
}})();
</script>
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

    # -- auth --------------------------------------------------------------- #

    def _presented_token(self) -> str:
        """Bearer header first, then ?token= for a browser that cannot set one."""
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() == "bearer" and presented:
            return presented.strip()
        return (self._query().get("token", [""])[0] or "").strip()

    def _query(self) -> dict:
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return parsed

    def _identify(self) -> dict:
        """Who is asking: {'role': 'admin'|'project'|'open', 'project_id': int|None}.

        Three worlds, resolved in this order:

        * no projects and no admin token  -> open (legacy single-tenant)
        * admin token                     -> admin, may see every project
        * project token                   -> project, may see only its own rows
        """
        db_path = self.server.db_path           # type: ignore[attr-defined]
        admin = self.server.auth_token          # type: ignore[attr-defined]
        presented = self._presented_token()

        if admin and presented and _constant_time_equal(presented, admin):
            return {"role": "admin", "project_id": None}
        project = project_for_token(db_path, presented) if presented else None
        if project is not None:
            return {"role": "project", "project_id": project["id"], "name": project["name"]}
        if not admin and project_count(db_path) == 0:
            return {"role": "open", "project_id": None}
        return {"role": "denied", "project_id": None}

    def _forbid(self, status: int, headline: str, detail: str) -> None:
        """HTML for a browser, JSON for a client. Never leaks the project list."""
        if "text/html" in self.headers.get("Accept", ""):
            self._send(status, render_error_page(status, headline, detail).encode("utf-8"),
                       "text/html; charset=utf-8")
        else:
            self._json(status, {"error": headline, "detail": detail})

    def _scope(self, identity: dict) -> tuple[int | None, int | None]:
        """Return (project_id_to_filter, error_status).

        An admin may pass ?project_id=N or see everything. A project token is
        *pinned* to its own id: a different id in the query string is a 403, not
        a silent downgrade.
        """
        requested = self._query().get("project_id", [None])[0]
        wanted: int | None = None
        if requested not in (None, "", "all"):
            try:
                wanted = int(requested)
            except ValueError:
                return None, 400
        if identity["role"] == "project":
            if wanted is not None and wanted != identity["project_id"]:
                return None, 403
            return identity["project_id"], None
        return wanted, None

    # -- routes ------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            identity = self._identify()
            if path == "/":
                query = urllib.parse.urlsplit(self.path).query
                location = "/dashboard" + (f"?{query}" if query else "")
                self._send(302, b"", "text/plain; charset=utf-8", {"Location": location})
            elif path == "/dashboard":
                self._dashboard(identity)
            elif path == "/budget-planner":
                self._planner(identity)
            elif path == "/api/summary":
                self._summary(identity)
            elif path == "/api/rules":
                self._rules(identity)
            else:
                self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # a query error must not kill the handler thread
            self._json(500, {"error": "internal error", "detail": str(exc)})

    def _dashboard(self, identity: dict) -> None:
        if identity["role"] == "denied":
            self._forbid(401, "Unauthorised",
                         "Send 'Authorization: Bearer <token>' or ?token=. "
                         "Ask a pipeline engineer for your project's token.")
            return
        project_id, error = self._scope(identity)
        if error == 400:
            self._forbid(400, "project_id must be a number", "Try ?project_id=1")
            return
        if error == 403:
            self._forbid(403, "That project is not yours",
                         "This token is scoped to a single project.")
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        name = None
        if project_id is not None:
            match = [p for p in list_projects(db_path) if p["id"] == project_id]
            if not match:
                self._forbid(404, "No such project", f"project_id={project_id} is not registered")
                return
            name = match[0]["name"]
        refresh = 0
        raw_refresh = self._query().get("refresh", ["0"])[0]
        try:
            refresh = max(0, min(int(raw_refresh), MAX_REFRESH_SECONDS))
        except (TypeError, ValueError):
            refresh = 0
        body = render_dashboard(db_path, project_id, name, refresh=refresh).encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _planner(self, identity: dict) -> None:
        if identity["role"] == "denied":
            self._forbid(401, "Unauthorised", "The budget planner needs a project or admin token.")
            return
        if identity["role"] == "open":
            self._forbid(403, "No projects yet",
                         "Create one on the server: --add-project <name>")
            return
        project_id, error = self._scope(identity)
        if error is not None:
            self._forbid(error if error != 400 else 400, "Bad project_id", "Try ?project_id=1")
            return
        if project_id is None:
            self._forbid(400, "Pick a project",
                         "Open /budget-planner?project_id=<id> with your project token.")
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        projects = list_projects(db_path)
        match = [p for p in projects if p["id"] == project_id]
        if not match:
            self._forbid(404, "No such project", f"project_id={project_id} is not registered")
            return
        body = render_planner(db_path, project_id, match[0]["name"],
                              self._presented_token(), projects).encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _summary(self, identity: dict) -> None:
        if identity["role"] == "denied":
            self._json(401, {"error": "unauthorised"})
            return
        project_id, error = self._scope(identity)
        if error is not None:
            self._json(error, {"error": "bad project scope"})
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        server = self.server  # type: ignore[attr-defined]
        self._json(200, {
            "project_id": project_id,
            "totals": totals(db_path, project_id),
            "bottlenecks": [{"rule": label, "count": count}
                            for label, count in bottlenecks(db_path, project_id=project_id)],
            # Operational visibility: how many alerts this run suppressed, and
            # the window that did it, so a lead can tell "quiet" from "muted".
            "alerts": {"enabled": server.notifier is not None,
                       "cooldown": server.alert_cooldown,
                       "suppressed": server.alerts_suppressed},
        })

    def _rules(self, identity: dict) -> None:
        """The published budget, in exactly the shape rules.toml uses.

        This is what `--remote-rules` points at, so a producer's click in the
        planner reaches every artist on their next run.
        """
        if identity["role"] == "denied":
            self._json(401, {"error": "unauthorised"})
            return
        project_id, error = self._scope(identity)
        if error is not None:
            self._json(error, {"error": "bad project scope"})
            return
        if project_id is None:
            self._json(400, {"error": "project_id is required",
                             "detail": "use ?project_id=<id>"})
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        self._json(200, budget_document(budget_for_project(db_path, project_id)))

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path not in ("/api/telemetry", "/api/save-budget"):
            self._json(404, {"error": "not found", "path": path})
            return
        payload = self._read_json()
        if payload is None:      # _read_json already answered the request
            return
        if path == "/api/telemetry":
            self._ingest(payload)
        else:
            self._save_budget(payload)

    def _read_json(self) -> dict | None:
        """Parse the body, or answer with the right 4xx and return None."""
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._json(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(length_header)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": f"body must be between 0 and {MAX_BODY_BYTES} bytes"})
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": "body is not valid JSON"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return payload

    def _ingest(self, payload: dict) -> None:
        identity = self._identify()
        if identity["role"] == "denied":
            self._json(401, {"error": "unauthorised: a project token is required",
                             "detail": "send 'Authorization: Bearer <project token>'"})
            return
        # In project mode the token decides the tenant; a project_id in the body
        # is ignored so a client can never write into someone else's rows.
        project_id = identity["project_id"]
        if identity["role"] == "open":
            requested = payload.get("project_id")
            project_id = requested if isinstance(requested, int) else None

        db_path = self.server.db_path  # type: ignore[attr-defined]
        try:
            verdict_id = store_verdict(db_path, payload, project_id)
        except ValueError as exc:   # shape problem: the client should fix it
            self._json(400, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._json(500, {"error": "database error", "detail": str(exc)})
            return

        self._maybe_alert(payload, project_id)
        self._json(201, {"stored": True, "id": verdict_id, "project_id": project_id})

    def _maybe_alert(self, payload: dict, project_id: int | None) -> None:
        """Push a webhook when a failure blows a budget by more than 50%.

        Throttled per scope so a nightly sweep failing 300 assets sends one
        message, not 300. The asset still lands in the database either way — the
        dashboard is the complete record, the webhook is the poke.
        """
        notifier = getattr(self.server, "notifier", None)
        if notifier is None:
            return
        if payload.get("passed") is not False:
            return   # healed assets pass; they are not pipeline emergencies
        server = self.server  # type: ignore[attr-defined]
        budget = budget_for_project(server.db_path, project_id)
        overshoot = overshoots(payload, budget)
        if not overshoot:
            return
        scope = alert_scope(project_id)
        if not claim_alert_slot(server.db_path, scope, server.alert_cooldown):
            server.alerts_suppressed += 1
            if not getattr(server, "quiet", False):
                print(f"asset_health: alert for {scope} suppressed by the "
                      f"{server.alert_cooldown:g}s cooldown", file=sys.stderr)
            return
        notifier.submit(alert_text(payload, budget, overshoot))

    def _save_budget(self, payload: dict) -> None:
        identity = self._identify()
        if identity["role"] in ("denied", "open"):
            self._json(401 if identity["role"] == "denied" else 403,
                       {"error": "a project or admin token is required to publish budgets"})
            return
        requested = payload.get("project_id")
        if identity["role"] == "project":
            requested = identity["project_id"]
        if not isinstance(requested, int):
            self._json(400, {"error": "project_id is required"})
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        if not any(row["id"] == requested for row in list_projects(db_path)):
            self._json(404, {"error": f"no such project: {requested}"})
            return
        try:
            saved = save_budget(db_path, requested, payload,
                                updated_by=payload.get("updated_by", "") or "")
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"saved": True, "project_id": requested,
                         "budget": {k: v for k, v in saved.items()
                                    if k not in ("updated_at", "updated_by")}})

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
                 quiet: bool = False, notifier: "Notifier | None" = None,
                 alert_cooldown: float = DEFAULT_ALERT_COOLDOWN):
        self.db_path = str(db_path)
        self.auth_token = token          # the studio-wide admin token, optional
        self.quiet = quiet
        self.notifier = notifier
        self.alert_cooldown = float(alert_cooldown)
        self.alerts_suppressed = 0       # visible in /api/summary for tuning
        super().__init__(address, Handler)


def build_server(host: str, port: int, db_path: str | Path,
                 token: str = "", quiet: bool = False,
                 notifier: "Notifier | None" = None,
                 alert_cooldown: float = DEFAULT_ALERT_COOLDOWN) -> AssetHealthServer:
    return AssetHealthServer((host, port), db_path, token, quiet, notifier, alert_cooldown)


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
    # One demo "healed" row, so the dashboard's healing card is not always zero.
    store_verdict(db_path, {
        "timestamp": datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat(timespec="seconds"),
        "hostname": "WS-ART-014", "username": "jdoe", "platform": "Windows-11",
        "name": "stone_wall_Albedo.png", "path": "env/stone_wall_Albedo.png",
        "size_bytes": 734_003, "passed": True, "status": "HEALED", "metric": "1024x1024",
        "problems": [], "modifications": ["padded 1000x1000 -> 1024x1024"],
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

    tenancy = parser.add_argument_group("projects (multi-tenant)")
    tenancy.add_argument("--add-project", metavar="NAME",
                         help="create a project, print its token once, and exit")
    tenancy.add_argument("--list-projects", action="store_true",
                         help="list projects and exit")

    alerts = parser.add_argument_group("alerting")
    alerts.add_argument("--webhook-url", default="",
                        help="POST critical alerts here (default: $SLACK_WEBHOOK_URL / "
                             "$DISCORD_WEBHOOK_URL / $TEAMS_WEBHOOK_URL)")
    alerts.add_argument("--webhook-provider", choices=("slack", "discord", "teams"),
                        default="", help="envelope format (default: inferred from the URL)")
    alerts.add_argument("--webhooks-config", metavar="PATH",
                        help="TOML/JSON file with a [webhooks] table")
    alerts.add_argument("--alert-cooldown", type=float, default=None, metavar="SECONDS",
                        help="minimum gap between alerts per project (default "
                             f"{DEFAULT_ALERT_COOLDOWN:g}s; 0 disables throttling)")
    return parser


WEBHOOK_ENV = (("SLACK_WEBHOOK_URL", "slack"),
               ("DISCORD_WEBHOOK_URL", "discord"),
               ("TEAMS_WEBHOOK_URL", "teams"))


def resolve_webhook(args: argparse.Namespace) -> tuple[str, str, float]:
    """Return (url, provider, cooldown) from CLI, env or a config file.

    Precedence per value: CLI flag > environment > config file > default.
    """
    url = (args.webhook_url or "").strip()
    provider = (args.webhook_provider or "").strip()
    cooldown = args.alert_cooldown
    file_cooldown = None

    if not url:
        for variable, name in WEBHOOK_ENV:
            if os.environ.get(variable):
                url, provider = os.environ[variable].strip(), provider or name
                break

    if not url and args.webhooks_config:
        path = Path(args.webhooks_config).expanduser()
        try:
            text = path.read_text(encoding="utf-8-sig")
            if path.suffix.lower() == ".json":
                document = json.loads(text)
            else:
                import tomllib

                document = tomllib.loads(text)
            table = document.get("webhooks", {}) if isinstance(document, dict) else {}
            url = url or str(table.get("url", "") or "")
            provider = provider or str(table.get("provider", "") or "")
            if table.get("cooldown") is not None:
                file_cooldown = float(table["cooldown"])
        except (OSError, ValueError) as exc:
            print(f"warning: webhooks config unreadable ({exc})", file=sys.stderr)

    if url and not provider:
        lowered = url.lower()
        provider = ("discord" if "discord" in lowered
                    else "teams" if "office.com" in lowered or "teams" in lowered
                    else "slack")
    if cooldown is None:
        cooldown = file_cooldown if file_cooldown is not None else DEFAULT_ALERT_COOLDOWN
    return url, provider or "slack", float(cooldown)


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

    # Management commands run and exit; they never bind a port.
    if args.list_projects:
        rows = list_projects(db_path)
        if not rows:
            print("no projects yet: --add-project <name>")
        for row in rows:
            print(f"{row['id']:>3}  {row['name']:<24} created {row['created_at']}")
        return 0
    if args.add_project:
        try:
            project_id, token = create_project(db_path, args.add_project)
        except (ValueError, sqlite3.IntegrityError) as exc:
            print(f"error: cannot create project: {exc}", file=sys.stderr)
            return 2
        print(f"created project {project_id}: {args.add_project}")
        print(f"  token      {token}   (shown once; only its hash is stored)")
        print(f"  dashboard  /dashboard?project_id={project_id}&token=<token>")
        print(f"  planner    /budget-planner?project_id={project_id}&token=<token>")
        print(f"  rules URL  /api/rules?project_id={project_id}  (for --remote-rules)")
        return 0

    seeded = False
    if not args.no_seed:
        try:
            seeded = seed_if_first_launch(db_path)
        except sqlite3.Error as exc:
            print(f"warning: seeding skipped ({exc})", file=sys.stderr)

    webhook_url, webhook_provider, alert_cooldown = resolve_webhook(args)
    notifier = Notifier(webhook_url, webhook_provider) if webhook_url else None

    try:
        httpd = build_server(args.host, args.port, db_path, args.token, args.quiet,
                             notifier, alert_cooldown)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2

    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    projects = list_projects(db_path)
    print(f"AssetHealth listening on http://{shown}:{port}/dashboard")
    print(f"  database   {db_path.resolve()}")
    print("  ingest     POST /api/telemetry")
    if projects:
        print(f"  projects   {len(projects)} registered - a project token is required")
        for row in projects:
            print(f"             {row['id']}: {row['name']}")
    else:
        print("  projects   none - single-tenant mode (no per-project isolation)")
        print("             create one with --add-project <name>")
    if args.token:
        print("  admin      Bearer token accepted (sees every project)")
    elif not projects:
        print("  auth       DISABLED - fine on loopback, not on a shared network")
    if notifier is not None:
        print(f"  alerts     {webhook_provider} -> {webhook_url}")
        print(f"             cooldown {alert_cooldown:g}s per project"
              f"{' (disabled)' if alert_cooldown <= 0 else ''}")
    if seeded:
        print(f"  seeded     {len(_SEED)} demo verdicts (first launch)")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
        if notifier is not None:
            notifier.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
