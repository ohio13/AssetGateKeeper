#!/usr/bin/env python3
r"""AssetHealth Server - the receiving end of the AssetGatekeeper ecosystem.

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
    fine for 127.0.0.1 and not fine for anything else - the server says so on boot.

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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
for _path in (str(HERE), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Only GET /api/rules needs the gate: it reuses the parser that *defines* a
# budget, so the inspector can never drift from what is actually enforced.
# Ingest and the dashboard stay pure stdlib - agk has no side effects on import.
import asset_gatekeeper as agk  # noqa: E402

# Storage: one abstraction over SQLite (the default) and pooled PostgreSQL.
# The call sites below are unchanged - they open a unit of work and close it.
from enterprise import analytics as enterprise_analytics  # noqa: E402
from enterprise.audit import NullLog, build_audit_log  # noqa: E402
from enterprise.config import ConfigError as EnterpriseConfigError  # noqa: E402
from server import netguard  # noqa: E402 - shared Host policy
from enterprise.config import load_config  # noqa: E402
from enterprise.store import (Store, StoreError, connection_is_sqlite,  # noqa: E402
                              is_dsn, open_store)

#: Everything a storage call can raise on either engine. Handlers used to catch
#: sqlite3.Error alone, so a PostgreSQL failure escaped as a bare 500 and the
#: reason - the only part an operator can act on - was lost.
STORAGE_ERRORS = (sqlite3.Error, StoreError)

#: SIEM forwarding. A NullLog by default: a studio that has not configured
#: forwarding should not get a surprise file on disk.
_AUDIT = NullLog()


def set_audit_log(log) -> None:
    """Install the audit log the server reports security events to."""
    global _AUDIT
    _AUDIT = log if log is not None else NullLog()


def audit_log():
    return _AUDIT

# --- #
# Configuration
# --- #

DEFAULT_HOST = "127.0.0.1"       # loopback by default: no accidental exposure
DEFAULT_PORT = 8080

#: Sent on every response. A dashboard that renders asset names and problem text
#: from artists is exactly the kind of page that must not be framed, sniffed or
#: allowed to phone home. Inline style/script stay allowed because the pages are
#: built from Python strings with their own <style> and <script> blocks; every
#: external origin is still refused, so an injected tag cannot load anything.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    # same-origin, not no-referrer: with no-referrer Chromium sends
    # "Origin: null" on a same-origin form POST, which the CSRF check then
    # refuses - that is what broke admin sign-in in every real browser. This
    # still sends nothing to another origin.
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; "
     "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
     "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
)

#: Host names that always belong to this machine: see server/netguard.py, which
#: both servers share (an IP literal, a loopback name or this box's own name).
DEFAULT_DB = "asset_health.db"
DEFAULT_TOKEN_ENV = "AUTH_TOKEN"
RULES_ENV = "ASSET_GATEKEEPER_RULES"    # same name the workstation uses
# Kept here only so --port-auto never walks onto the other service's port; the
# workstation does not import this module, so the number is duplicated on purpose.
ART_WORKSTATION_PORT = 8100
# Semicolon-separated on Windows, colon on POSIX - os.pathsep handles both. The
# launcher sets this so a studio can widen the project allow-list without a CLI.
ALLOW_ROOTS_ENV = "AGK_PROJECT_ROOTS"

MAX_BODY_BYTES = 64 * 1024       # one verdict is ~1 KB; 64 KB is already generous

# How much of a refused request body is read and discarded before the connection
# is dropped instead. Draining is what stops an unread body from being parsed as
# the next request; the cap is what stops a 4 GB Content-Length from tying up a
# thread. Above it the connection is closed, which is unambiguous.
DRAIN_HARD_CAP = 8 * 1024 * 1024
WALL_OF_SHAME_LIMIT = 20
BOTTLENECK_LIMIT = 8

UTC_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"


# --- #
# Database
# --- #

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
    -- PASSED / HEALED / FAILED / ERROR. A healed asset passed, but the studio
    -- still wants a record that the file was rewritten on its way in. An ERROR
    -- row means the file could not be read at all, which is not a verdict on the
    -- art and must not be read as one.
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

-- -
-- Admin: who may change a budget, and the record of every change they made.
-- Only a PBKDF2 hash is stored -- the database never holds a password.
-- -
CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role          TEXT NOT NULL,          -- director | producer | artist
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT '',
    disabled      INTEGER NOT NULL DEFAULT 0
);

-- Sessions store the hash of the cookie token, so a stolen database is not a
-- stolen login. csrf is bound to the session for double-submit protection.
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    csrf       TEXT NOT NULL DEFAULT ''
);

-- The audit trail: one row per changed key, with both sides of the change.
CREATE TABLE IF NOT EXISTS rule_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    user_id    INTEGER,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT '',
    target     TEXT NOT NULL,             -- mesh.max_triangles, tags.boss.max_triangles
    summary    TEXT NOT NULL DEFAULT '',
    old_value  TEXT NOT NULL DEFAULT '',
    new_value  TEXT NOT NULL DEFAULT '',
    source_ip  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_audit_when ON rule_audit(changed_at);

-- Projects the admin page has opened, so the picker can offer them again.
CREATE TABLE IF NOT EXISTS project_roots (
    path      TEXT PRIMARY KEY,
    label     TEXT NOT NULL DEFAULT '',
    last_used TEXT NOT NULL
);

-- -
-- Multi-tenancy. One row per game project or outsource vendor: they share a
-- server, never a row.
-- -
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
-- Expiring exceptions (G1), published with the project's budget.
CREATE TABLE IF NOT EXISTS exceptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path       TEXT    NOT NULL,
    rule       TEXT    NOT NULL,
    allow_json TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    owner      TEXT    NOT NULL,
    expires    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    created_by TEXT    NOT NULL DEFAULT ''
);

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


#: One Store per target, created once. A PostgreSQL pool per request would be a
#: connection storm, and re-opening the SQLite file for every call buys nothing.
_STORES: dict[str, Store] = {}
_STORES_LOCK = threading.Lock()


def store_for(db_path: str | Path) -> Store:
    """The Store for this target - a SQLite path or a postgresql:// DSN."""
    key = str(db_path)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = open_store(key)
            _STORES[key] = store
        return store


def close_stores() -> None:
    """Close every pooled connection (explicit shutdown, and in tests)."""
    with _STORES_LOCK:
        for store in _STORES.values():
            with contextlib.suppress(Exception):
                store.close()
        _STORES.clear()


@contextlib.contextmanager
def open_db(db_path: str | Path):
    """One short-lived unit of work, on SQLite or on pooled PostgreSQL.

    SQLite gets a fresh connection per unit of work - cheap, and it keeps WAL
    readers concurrent without sharing a handle across threads. PostgreSQL gets a
    pooled connection, because one connection per request would defeat the pool.
    Neither leaks: ``sqlite3.Connection`` as a context manager commits but never
    closes, which is exactly the trap this wrapper exists to avoid.
    """
    with store_for(db_path).session() as connection:
        yield connection


def insert_id(connection, sql: str, params: tuple) -> int:
    """Insert one row and return its id, on either backend.

    SQLite exposes ``lastrowid`` and PostgreSQL does not, so the same statement
    gets a ``RETURNING id`` clause there. One helper means the row-creating
    handlers cannot drift into two dialects.
    """
    if connection_is_sqlite(connection):
        return int(connection.execute(sql, params).lastrowid or 0)
    row = connection.execute(sql.rstrip().rstrip(";") + " RETURNING id",
                             params).fetchone()
    return int(row["id"]) if row else 0


def table_columns(connection, table: str) -> set[str]:
    """Column names without PRAGMA: PostgreSQL answers from information_schema."""
    if connection_is_sqlite(connection):
        return {row[1] for row in
                connection.execute(f"PRAGMA table_info({table})").fetchall()}
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        (table,)).fetchall()
    return {row["column_name"] for row in rows}


def init_db(db_path: str | Path) -> None:
    """Create the schema on first run, and migrate a pre-existing database.

    ``CREATE TABLE IF NOT EXISTS`` silently does nothing to a table that already
    exists, so a database written before healing shipped would never gain the
    ``status`` column. ALTER it explicitly instead of asking people to delete
    their metrics.
    """
    with open_db(db_path) as connection:
        if connection_is_sqlite(connection):
            connection.execute("PRAGMA journal_mode = WAL")  # concurrent readers
        connection.executescript(SCHEMA)
        columns = table_columns(connection, "verdicts")
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
    if status not in ("PASSED", "HEALED", "FAILED", "ERROR"):
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


def _presented_hint(presented: str) -> str:
    """An actor label for the audit trail that is not the credential itself."""
    if not presented:
        return "anonymous"
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()[:8]
    return f"token:{digest}"


def store_verdict(db_path: str | Path, payload: dict,
                  project_id: int | None = None) -> int:
    """Insert one verdict plus its problems atomically. Returns the new row id."""
    row = _normalise(payload)
    with _WRITE_LOCK, open_db(db_path) as connection:
        verdict_id = insert_id(
            connection,
            "INSERT INTO verdicts (timestamp, received_at, hostname, username, os,"
            " filename, filepath, filesize, passed, metric, status, project_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["timestamp"], row["received_at"], row["hostname"], row["username"],
             row["os"], row["filename"], row["filepath"], row["filesize"],
             row["passed"], row["metric"], row["status"], project_id),
        )
        connection.executemany(
            "INSERT INTO problems (verdict_id, message) VALUES (?, ?)",
            [(verdict_id, message) for message in row["problems"]],
        )
        connection.executemany(
            "INSERT INTO modifications (verdict_id, action) VALUES (?, ?)",
            [(verdict_id, action) for action in row["modifications"]],
        )
    return verdict_id


# --- #
# Tenancy: projects, tokens, published budgets
# --- #

def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_project(db_path: str | Path, name: str, token: str | None = None) -> tuple[int, str]:
    """Add a project. Returns (id, token) - the token is shown once and hashed.

    Only the hash is stored: a metrics database that leaks is bad, a metrics
    database that also leaks every vendor's token is a breach.
    """
    if not name or not name.strip():
        raise ValueError("project name must not be empty")
    token = token or secrets.token_urlsafe(24)
    with _WRITE_LOCK, open_db(db_path) as connection:
        project_id = insert_id(
            connection,
            "INSERT INTO projects (name, api_token, created_at) VALUES (?, ?, ?)",
            (name.strip(), hash_token(token), _utc_now()),
        )
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


# --- published budgets - #

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


def list_exceptions(db_path: str | Path, project_id: int) -> list[dict]:
    """A project's published exceptions, soonest to end first."""
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM exceptions WHERE project_id = ? ORDER BY expires, id",
            (project_id,)).fetchall()
    return [{"id": row["id"], "path": row["path"], "rule": row["rule"],
             "allow": json.loads(row["allow_json"]), "reason": row["reason"],
             "owner": row["owner"], "expires": row["expires"],
             "created_at": row["created_at"], "created_by": row["created_by"]}
            for row in rows]


def add_exception(db_path: str | Path, project_id: int, entry: dict, created_by: str = "") -> dict:
    """Validate with the gate's own parser, so the dashboard can never publish an
    exception the gate would refuse to load."""
    if not isinstance(entry, dict):
        raise ValueError("an exception must be an object")
    fields = {key: entry.get(key) for key in ("path", "rule", "allow", "reason", "owner",
                                               "expires")}
    try:
        parsed = agk.parse_exceptions({"exceptions": [fields]}, where="exception")[0]
    except agk.ConfigError as exc:
        raise ValueError(str(exc)) from exc
    with open_db(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO exceptions (project_id, path, rule, allow_json, reason, owner, expires,"
            " created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, parsed.path, parsed.rule, json.dumps(parsed.allow), parsed.reason,
             parsed.owner, parsed.expires.isoformat(), _utc_now(), str(created_by)[:80]))
        new_id = cursor.lastrowid
    return next(item for item in list_exceptions(db_path, project_id) if item["id"] == new_id)


def remove_exception(db_path: str | Path, project_id: int, exception_id: int) -> bool:
    with open_db(db_path) as connection:
        return connection.execute("DELETE FROM exceptions WHERE id = ? AND project_id = ?",
                                  (exception_id, project_id)).rowcount > 0


def budget_document(budget: dict, exceptions: "list[dict] | tuple" = ()) -> dict:
    """The saved budget as a rules document - exactly what `--remote-rules` eats.

    Same shape as rules.toml, so a client never has to learn a second format and
    the gate's own parser validates it on the way in.
    """
    document = _budget_tables(budget)
    if exceptions:
        document["exceptions"] = [
            {key: item[key] for key in ("path", "rule", "allow", "reason", "owner", "expires")}
            for item in exceptions]
    return document


def _budget_tables(budget: dict) -> dict:
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


# --- #
# Dashboard queries
# --- #

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
            "filepath": row["filepath"],
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
    """Which rule is broken most often - the ranking that decides next sprint."""
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


# --- #
# Alerting: how badly over budget, and a notifier that cannot slow ingest
# --- #

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
    return (f"- CRITICAL PIPELINE ALERT: User {payload.get('username') or 'unknown'} on "
            f"{payload.get('hostname') or 'unknown'} failed validation for "
            f"{payload.get('name') or payload.get('filename') or 'unknown'}. "
            f"Reasons: {reasons}. Over budget: {details}")


# --- #
# Tell the artist, not just the channel (G7)
# --- #

PEOPLE_ENV = "AGK_PEOPLE"


@dataclass(frozen=True)
class Person:
    """Someone a failure can be addressed to: their chat handle, and the user
    and machine names their gate reports with."""

    key: str
    name: str
    chat: str                 # Slack member ID, Discord user ID, or Teams email
    users: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()


def load_people(path: str | Path) -> list[Person]:
    """Read people.toml (or .json): one [people.<key>] table per person."""
    path = Path(path).expanduser()
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        document = json.loads(text)
    else:
        document = agk.parse_rules_text(text, origin=str(path))
    table = document.get("people", {}) if isinstance(document, dict) else None
    if not isinstance(table, dict) or not table:
        raise ValueError(f"{path}: expected [people.<name>] tables")
    people = []
    for key, entry in table.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: [people.{key}] must be a table")
        unknown = set(entry) - {"name", "chat", "users", "hosts"}
        if unknown:
            raise ValueError(f"{path}: [people.{key}] has unknown key(s) {', '.join(sorted(unknown))}")
        chat = str(entry.get("chat", "") or "").strip()
        if not chat:
            raise ValueError(f"{path}: [people.{key}] needs chat (Slack member ID, Discord "
                             "user ID or Teams email)")
        users = entry.get("users", [])
        hosts = entry.get("hosts", [])
        if not isinstance(users, list) or not isinstance(hosts, list) or not (users or hosts):
            raise ValueError(f"{path}: [people.{key}] needs users and/or hosts lists")
        people.append(Person(key=str(key), name=str(entry.get("name") or key), chat=chat,
                             users=tuple(str(item).strip().lower() for item in users),
                             hosts=tuple(str(item).strip().lower() for item in hosts)))
    return people


def person_for(people: "list[Person] | tuple", username: str = "", hostname: str = "") -> Person | None:
    """The user name wins (it is the artist); the machine name is the fallback."""
    user, host = str(username or "").strip().lower(), str(hostname or "").strip().lower()
    for person in people or ():
        if user and user in person.users:
            return person
    for person in people or ():
        if host and host in person.hosts:
            return person
    return None


def mention_for(provider: str, person: Person | None) -> str:
    if person is None:
        return ""
    provider = (provider or "slack").lower()
    if provider == "teams":
        return f"<at>{person.name}</at>"
    return f"<@{person.chat}>"     # Slack and Discord both mention by id


def workstation_link(base: str, path: str, name: str) -> str:
    """A link that opens the asset in the Art Workstation (?find= picks the newest
    version whose depot path or name matches; nothing is looked up here)."""
    if not base:
        return ""
    target = str(path or name or "")
    return f"{base.rstrip('/')}/viewer?find={urllib.parse.quote(target, safe='')}" if target else ""


def webhook_body(provider: str, text: str, person: Person | None = None) -> bytes:
    """Slack, Discord and Teams all want a slightly different envelope."""
    provider = (provider or "slack").lower()
    if provider == "teams" and person is not None:
        # A Teams mention only works inside an Adaptive Card, with the person
        # named in msteams.entities; a MessageCard prints "<at>" as text.
        body = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "version": "1.4",
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                        "msteams": {"entities": [{
                            "type": "mention", "text": f"<at>{person.name}</at>",
                            "mentioned": {"id": person.chat, "name": person.name}}]}}}]}
        return json.dumps(body).encode("utf-8")
    if provider == "discord":
        body = {"content": text, "username": "AssetGatekeeper"}
    elif provider == "teams":
        body = {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                "summary": "AssetGatekeeper alert", "themeColor": "D13438", "text": text}
    else:  # slack (also the generic "something that takes {\"text\": ...}")
        body = {"text": text}
    return json.dumps(body).encode("utf-8")


def digest_text(rows: list[dict], person: Person | None, provider: str,
                workstation_url: str = "") -> str:
    """One message for one person's failures since the last digest."""
    who = mention_for(provider, person) if person else "Unassigned"
    lines = [f"{who} - {len(rows)} asset{'s' if len(rows) != 1 else ''} failed the gate "
             "since the last digest:"]
    for row in rows[:20]:
        reason = (row.get("reasons") or "no reason recorded").split("\n")[0]
        link = workstation_link(workstation_url, row.get("filepath", ""), row.get("filename", ""))
        lines.append(f"- {row['filename']}: {reason}" + (f" ({link})" if link else ""))
    if len(rows) > 20:
        lines.append(f"- ... and {len(rows) - 20} more on the dashboard")
    return "\n".join(lines)


def send_digest(db_path: str | Path, notifier: "Notifier", people: "list[Person] | tuple",
                since: str, workstation_url: str = "") -> int:
    """Group the failures received after ``since`` by person and send one
    message each (and one for anyone unmatched). Returns messages sent."""
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT v.id, v.filename, v.filepath, v.username, v.hostname,"
            " (SELECT group_concat(p.message, char(10)) FROM problems p WHERE p.verdict_id = v.id)"
            " AS reasons FROM verdicts v WHERE v.passed = 0 AND v.received_at > ?"
            " ORDER BY v.id", (since,)).fetchall()
    groups: dict = {}
    for row in rows:
        person = person_for(people, row["username"], row["hostname"])
        groups.setdefault(person, []).append(dict(row))
    for person, items in groups.items():
        notifier.submit(digest_text(items, person, notifier.provider, workstation_url), person)
    return len(groups)


def _digest_loop(server) -> None:
    """Sleep until HH:MM local time, send the digest, repeat."""
    last = _utc_now()
    while not server.stopping:
        hour, minute = server.digest_at
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        while not server.stopping and datetime.now() < target:
            time.sleep(min(30.0, max(0.5, (target - datetime.now()).total_seconds())))
        if server.stopping:
            return
        started = _utc_now()
        try:
            send_digest(server.db_path, server.notifier, server.people, last,
                        server.workstation_url)
        except Exception as exc:     # a digest must never take the server down
            print(f"asset_health: digest failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        last = started


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


class Broadcaster:
    """Fan-out of live events to connected dashboards (Server-Sent Events).

    Every subscriber gets its own bounded queue: one slow browser tab must never
    stall the ingest thread, and a tab that stops reading must not grow memory.
    """

    def __init__(self, queue_max: int = 100) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._queue_max = queue_max
        self.published = 0

    def subscribe(self) -> "queue.Queue[dict]":
        channel: "queue.Queue[dict]" = queue.Queue(maxsize=self._queue_max)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: "queue.Queue[dict]") -> None:
        with self._lock:
            self._subscribers.discard(channel)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: dict) -> None:
        """Never blocks, never raises: a dead tab is not an error."""
        with self._lock:
            channels = list(self._subscribers)
        self.published += 1
        for channel in channels:
            try:
                channel.put_nowait(event)
            except queue.Full:
                # Drop the oldest for this one subscriber rather than skipping the
                # newest: the dashboard cares about the latest state, not history.
                try:
                    channel.get_nowait()
                    channel.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


class Notifier:
    """Fire-and-forget webhook POSTs. Same three rules as the gate's telemetry.

    1. Never raise - a dead webhook must not fail an ingest.
    2. Never block - `submit()` enqueues; one daemon thread owns the sockets.
    3. Never grow without bound - a capped queue drops and counts.
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

    def submit(self, text: str, person: Person | None = None) -> None:
        if self._closed:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ah-notifier", daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait((text, person))
        except queue.Full:
            self.dropped += 1
            print("asset_health: webhook queue full, dropped an alert", file=sys.stderr)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            text, person = item if isinstance(item, tuple) else (item, None)
            self._post(text, person)

    def _post(self, text: str, person: Person | None = None) -> None:
        request = urllib.request.Request(
            self.url, data=webhook_body(self.provider, text, person), method="POST",
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
        called on the ingest path - that path must not wait for a network.
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


# --- #
# HTML
# --- #

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
.exceptions { width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0; }
.exceptions th, .exceptions td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
.exceptions td.warn { color: #e0af68; } .exceptions td.err { color: #ff6b6b; }
.exception-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
.exception-form input { font: inherit; color: var(--text); background: #0c1017;
  border: 1px solid var(--line); border-radius: 8px; padding: 9px 12px; min-width: 0; }
#exceptions h2 { margin: 0 0 6px; font-size: 15px; }
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


#: One page of the full audit. 50 rows keeps the HTML small enough to render in
#: a frame even when the database holds tens of thousands of verdicts.
AUDIT_PAGE_SIZE = 50
#: Ceiling on how many rows are loaded to resolve tag filters. Tags are derived
#: from the path (they are not stored per verdict), so filtering happens in
#: Python - bounded on purpose rather than letting a 200k-row table eat memory.
AUDIT_SCAN_LIMIT = 20_000


def resolve_tags(path: str, scan_root: str | Path, tag_names: list[str]) -> list[str]:
    """Which configured categories this verdict's file falls into."""
    if not path or not tag_names:
        return []
    known = {agk.normalise_tag(name): name for name in tag_names}
    matched: list[str] = []
    try:
        for candidate in agk.tag_candidates(Path(path), Path(scan_root)):
            name = known.get(agk.normalise_tag(candidate))
            if name and name not in matched:
                matched.append(name)
    except (OSError, ValueError):
        return []
    return matched[:3]


def audit_query(db_path: str | Path, statuses: tuple[str, ...],
                project_id: int | None = None) -> list[dict]:
    """Every matching verdict, newest first (the caller pages it)."""
    clauses, params = [], []
    if statuses:
        clauses.append(f"passed IN ({','.join('?' * len(statuses))})")
        params.extend(1 if item == "pass" else 0 for item in statuses)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(int(project_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (f"SELECT id, filename, filepath, passed, status, hostname, received_at "
           f"FROM verdicts {where} ORDER BY id DESC LIMIT ?")
    params.append(AUDIT_SCAN_LIMIT)
    try:
        with open_db(db_path) as connection:
            rows = [dict(row) for row in connection.execute(sql, params)]
            counts = {True: 0, False: 0}
            for label, passed in connection.execute(
                    f"SELECT 1, COUNT(*) FROM verdicts {where} "
                    f"GROUP BY passed", params[:-1]):
                counts[bool(label)] = int(passed)
    except STORAGE_ERRORS:
        return [], {True: 0, False: 0}
    return rows, counts


def audit_page(db_path: str | Path, scan_root: str | Path, tag_names: list[str],
               statuses: tuple[str, ...], wanted_tags: list[str],
               page: int, project_id: int | None = None) -> dict:
    rows, counts = audit_query(db_path, statuses, project_id)
    for row in rows:
        row["tags"] = resolve_tags(row.get("filepath") or "", scan_root, tag_names)
    if wanted_tags:
        wanted = {agk.normalise_tag(item) for item in wanted_tags}
        rows = [row for row in rows
                if wanted & {agk.normalise_tag(tag) for tag in row["tags"]}]
    total = len(rows)
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * AUDIT_PAGE_SIZE
    groups: dict[str, list[dict]] = {}
    for row in rows[start:start + AUDIT_PAGE_SIZE]:
        groups.setdefault(row["tags"][0] if row["tags"] else "Uncategorised", []).append(row)
    return {"rows": rows[start:start + AUDIT_PAGE_SIZE], "groups": groups,
            "total": total, "pages": pages, "page": page, "counts": counts}

REVIEW_WORDS = {"in_review": "In review", "changes_requested": "Changes requested",
                "approved": "Approved"}


def render_audit_page(data: dict, *, title: str, status_filter: str,
                      tag_names: list[str], selected: list[str],
                      base_path: str, reviews: dict | None = None) -> str:
    """The full audit: every verdict, paginated, grouped by category."""
    def href(page: int, status: str = status_filter) -> str:
        params = [f"status={urllib.parse.quote(status)}", f"page={page}"]
        params += [f"tag={urllib.parse.quote(item)}" for item in selected]
        return f"{base_path}?{'&'.join(params)}"

    def row_html(entry: dict) -> str:
        state = "passed" if entry["passed"] else "failed"
        # An unreadable file is not a failing asset: say ERROR where we can.
        label = entry.get("status") if entry.get("status") == "ERROR" else state.upper()
        when = (entry.get("received_at") or "")[:19]
        return (
            f'<tr><td class="when">{_e(when)}</td>'
            f'<td class="file">{_e(entry.get("filename") or "")}'
            f'<div class="path">{_e((entry.get("filepath") or "")[:120])}</div></td>'
            f'<td><span class="badge {state}">{_e(label)}</span>{_review_chip(entry, reviews)}</td>'
            f'<td class="act">{_send_button(entry)}</td></tr>')

    groups = "".join(
        f"<details class='tree' open><summary>{_e(name)} "
        f"<span class='count'>{len(items)}</span></summary>"
        f"<table><tbody>{''.join(row_html(item) for item in items)}</tbody></table>"
        f"</details>"
        for name, items in sorted(data["groups"].items()))

    if not groups:
        groups = "<div class='panel'><div class='empty'>Nothing matches those filters.</div></div>"

    tag_boxes = "".join(
        f"<label class='check'><input type='checkbox' name='tag' value='{_e(name)}'"
        f"{' checked' if name in selected else ''}> {_e(name)}</label>"
        for name in tag_names) or "<span class='sub'>no categories configured</span>"

    status_options = "".join(
        f"<option value='{value}'{' selected' if value == status_filter else ''}>{label}</option>"
        for value, label in (("all", "All verdicts"), ("fail", "Failures only"),
                             ("pass", "Passes only")))

    pages = []
    for page in range(1, data["pages"] + 1):
        if data["pages"] > 9 and abs(page - data["page"]) > 3 and page not in (1, data["pages"]):
            if pages and pages[-1] != "...":
                pages.append("...")
            continue
        pages.append(page)
    pager = " ".join(
        "<span class='sub'>...</span>" if item == "..." else
        f"<a class='page{' active' if item == data['page'] else ''}' href='{href(item)}'>{item}</a>"
        for item in pages)

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{_e(title)} - AssetGatekeeper</title>
<style>
body{{margin:0;background:#0d1016;color:#e6e9ef;font:14px/1.5 ui-sans-serif,"Segoe UI",sans-serif}}
header{{padding:18px 24px;border-bottom:1px solid #262f3d;display:flex;gap:14px;align-items:baseline}}
h1{{font-size:17px;margin:0}} a{{color:#7aa2f7;text-decoration:none}} a:hover{{text-decoration:underline}}
main{{padding:20px 24px;max-width:1100px}}
form.filters{{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start;margin-bottom:18px;
  background:#161b24;border:1px solid #262f3d;border-radius:10px;padding:12px}}
select,input[type=submit],button{{background:#1d2431;color:#e6e9ef;border:1px solid #262f3d;
  border-radius:7px;padding:6px 10px;font:inherit;cursor:pointer}}
details.picker{{position:relative}} details.picker>summary{{list-style:none;cursor:pointer;
  background:#1d2431;border:1px solid #262f3d;border-radius:7px;padding:6px 10px}}
details.picker .menu{{position:absolute;z-index:5;background:#161b24;border:1px solid #262f3d;
  border-radius:8px;padding:8px;margin-top:4px;min-width:190px}}
label.check{{display:block;padding:2px 0;font-size:13px;white-space:nowrap}}
details.tree{{border:1px solid #262f3d;border-radius:10px;margin-bottom:12px;overflow:hidden}}
details.tree>summary{{padding:9px 13px;cursor:pointer;background:#161b24;font-weight:600}}
details.tree .count{{color:#8b94a7;font-weight:400;margin-left:6px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:7px 13px;border-top:1px solid #1e2530;vertical-align:top}}
td.when{{color:#8b94a7;white-space:nowrap;width:150px}}
.path{{color:#8b94a7;font-size:11px;word-break:break-all}}
.badge{{padding:1px 7px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.05em}}
.badge.failed{{background:rgba(255,107,107,.16);color:#ff6b6b}}
.badge.passed{{background:rgba(62,207,142,.16);color:#3ecf8e}}
.review{{font-size:11px;margin-top:4px;color:#8b94a7}}
.review.approved{{color:#3ecf8e}} .review.changes_requested{{color:#e0af68}}
button.send{{font-size:11px;padding:3px 8px}}
.page{{display:inline-block;padding:3px 9px;border:1px solid #262f3d;border-radius:6px;margin-right:3px}}
.page.active{{background:#1d2431;border-color:#7aa2f7}}
.empty,.sub{{color:#8b94a7}} .panel{{background:#161b24;border:1px solid #262f3d;border-radius:10px;padding:14px}}
</style></head><body>
<header><h1>{_e(title)}</h1>
<span class="sub">{data['total']} matching  page {data['page']} of {data['pages']}</span>
<a href="/dashboard">dashboard</a>
<a href="/audit/full">all verdicts</a>
<a href="/audit/passes">passes</a>
</header>
<main>
<form class="filters" method="get" action="{base_path}">
  <select name="status">{status_options}</select>
  <details class="picker"><summary>Categories ({len(selected)} selected)</summary>
    <div class="menu">{tag_boxes}</div></details>
  <input type="submit" value="Apply filters">
  <a href="{base_path}" class="sub">clear</a>
</form>
{groups}
<div class="pager">{pager}</div>
</main>
<script src="/static/dashboard.js" defer></script>
</body></html>"""

def _review_chip(entry: dict, reviews: dict | None) -> str:
    """The Art Workstation's sign-off for this file, when it has one (W2)."""
    if not reviews:
        return ""
    status = (reviews.get(("path", str(entry.get("filepath") or "").replace("\\", "/").lower()))
              or reviews.get(("name", str(entry.get("filename") or "").lower())))
    if not status:
        return ""
    return (f'<div class="review {_e(status)}" title="Art Workstation sign-off">'
            f'{_e(REVIEW_WORDS.get(status, status))}</div>')


def _send_button(entry: dict) -> str:
    """The one-click hand-off to the Art Workstation, for a single row.

    Only rows with a recorded path can be sent: a verdict from a machine whose
    files are not reachable here would produce a broken import, and guessing a
    path is exactly the kind of silent wrongness this tool avoids.
    """
    if not entry.get("filepath"):
        return '<span class="sub">no path recorded</span>'
    name = _e(entry.get("filename") or "asset")
    return (f'<button class="send" data-verdict="{int(entry["id"])}"'
            f' title="Ingest only {name} into the Art Workstation">'
            f"Send to Workstation</button>")

# --- #
# Analytics: the same numbers the CLI reports, rendered for a browser
# --- #

#: Windows a producer actually asks for. Deliberately short: "since 2019" is a
#: data-warehouse question, not a dashboard one.
ANALYTICS_WINDOWS = (("7d", "7 days"), ("30d", "30 days"), ("90d", "90 days"),
                     ("12m", "12 months"), ("3y", "3 years"))
ANALYTICS_BUCKETS = ("day", "week", "month")

ANALYTICS_CSS = """
:root { --bg:#0e1116; --panel:#151a21; --line:#232b36; --ink:#e6edf7; --muted:#8b97a8;
        --accent:#7aa2f7; --good:#3ecf8e; --bad:#ff6b6b; --warn:#e0af68; }
* { box-sizing:border-box; }
body { margin:0; padding:28px 26px 60px; background:var(--bg); color:var(--ink);
       font:14px/1.55 ui-sans-serif,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:1180px; margin:0 auto; }
header { display:flex; align-items:baseline; gap:12px; }
h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.09em;
     color:var(--muted); margin:26px 0 8px; }
.sub { color:var(--muted); font-size:12px; }
nav { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin:14px 0 4px; }
nav a { color:var(--accent); text-decoration:none; font-size:13px; }
nav a:hover { text-decoration:underline; }
nav a.on { color:var(--ink); font-weight:600; }
.chips { color:var(--muted); font-size:12px; }
.cards { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:10px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
        padding:12px 14px; }
.card .k { color:var(--muted); font-size:11px; text-transform:uppercase;
           letter-spacing:.06em; }
.card .v { font-size:24px; font-variant-numeric:tabular-nums; margin-top:4px; }
.accent { color:var(--accent); } .good { color:var(--good); } .bad { color:var(--bad); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
         padding:6px 14px 12px; margin-top:8px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--muted); border-bottom:1px solid var(--line); padding:8px 6px; }
td { padding:7px 6px; border-bottom:1px solid var(--line); vertical-align:top;
     font-variant-numeric:tabular-nums; }
tr:last-child td { border-bottom:none; }
.bar { height:8px; border-radius:4px; background:#1d2430; min-width:90px; }
.bar > span { display:block; height:8px; border-radius:4px; background:var(--good); }
.bar.warn > span { background:var(--warn); } .bar.bad > span { background:var(--bad); }
.empty { color:var(--muted); padding:14px 4px; }
.mono { font-family:ui-monospace,Consolas,monospace; font-size:12px; color:var(--muted); }
.note { color:var(--muted); font-size:12px; margin:6px 2px; }
footer { margin-top:32px; color:var(--muted); font-size:11px; }
"""


def analytics_report(db_path, project_id: int | None = None, since: str = "90d",
                     bucket: str = "week") -> dict:
    """One JSON document: compliance, growth and errors for the same window.

    The three sections answer different questions and fail differently - an empty
    error list is good news, an empty growth chart means the verdicts carried no
    triangle count - so each carries its own reason string instead of a silent
    zero.
    """
    # Reads go through the replica-aware view: a 2-year trend query has no
    # business competing with ingest for the primary.
    store = store_for(db_path).reader()
    report: dict = {"window": {"since": since, "bucket": bucket,
                               "project_id": project_id}}
    try:
        trend = enterprise_analytics.compliance_trend(
            store, bucket=bucket, since=since, project_id=project_id)
        report["compliance"] = trend.as_dict()
        report["compliance"]["delta"] = trend.delta
        report["summary"] = enterprise_analytics.compliance_summary(
            store, since=since, project_id=project_id)
    except Exception as exc:                      # a broken query is reported, not fatal
        report["compliance"] = {"points": [], "note": f"unavailable: {exc}"}
        report["summary"] = {}
    try:
        growth = enterprise_analytics.polygon_growth(
            store, bucket=bucket, since=since, project_id=project_id)
        report["growth"] = growth.as_dict()
    except enterprise_analytics.AnalyticsError as exc:
        report["growth"] = {"points": [], "note": str(exc)}
    try:
        clusters = enterprise_analytics.error_frequency(
            store, since=since, project_id=project_id, limit=15)
        report["errors"] = [cluster.as_dict() for cluster in clusters]
    except Exception as exc:
        report["errors"] = []
        report["errors_note"] = f"unavailable: {exc}"
    return report


def _analytics_href(since: str, bucket: str, project_id: int | None) -> str:
    params = {"since": since, "bucket": bucket}
    if project_id:
        params["project_id"] = str(project_id)
    return "/analytics?" + urllib.parse.urlencode(params)


def render_analytics_page(db_path, project_id: int | None = None,
                          project_name: str = "", since: str = "90d",
                          bucket: str = "week") -> str:
    report = analytics_report(db_path, project_id, since, bucket)
    summary = report.get("summary") or {}
    points = report.get("compliance", {}).get("points", [])
    growth = report.get("growth", {})
    growth_points = growth.get("points", [])
    clusters = report.get("errors", [])

    scope = f"project: {_e(project_name)}" if project_name else "all projects"

    window_links = "".join(
        f'<a class="{"on" if key == since else ""}" '
        f'href="{_analytics_href(key, bucket, project_id)}">{label}</a>'
        for key, label in ANALYTICS_WINDOWS)
    bucket_links = "".join(
        f'<a class="{"on" if key == bucket else ""}" '
        f'href="{_analytics_href(since, key, project_id)}">{key}</a>'
        for key in ANALYTICS_BUCKETS)

    compliance_value = summary.get("compliance")
    compliance_text = (f"{compliance_value * 100:.1f}%" if compliance_value is not None
                       else "-")
    compliance_class = ("good" if (compliance_value or 0) >= 0.9
                        else "accent" if (compliance_value or 0) >= 0.7 else "bad")
    delta = summary.get("trend") or 0
    delta_text = f"{delta * 100:+.1f} pts" if summary.get("buckets") else "-"

    if points:
        rows = []
        for point in points:
            share = point["compliance"] * 100
            tone = "good" if share >= 90 else ("warn" if share >= 70 else "bad")
            rows.append(
                "<tr>"
                f'<td class="mono">{_e(point["bucket"])}</td>'
                f'<td>{point["total"]:,}</td>'
                f'<td>{point["passed"]:,}</td>'
                f'<td>{point["healed"]:,}</td>'
                f'<td>{point["failed"]:,}</td>'
                f'<td style="width:34%"><div class="bar {tone}"><span '
                f'style="width:{share:.1f}%"></span></div></td>'
                f"<td>{share:.1f}%</td>"
                "</tr>")
        compliance_html = (
            '<div class="panel"><table><thead><tr><th>Bucket</th><th>Total</th>'
            "<th>Passed</th><th>Healed</th><th>Failed</th><th>Compliance</th>"
            "<th>Rate</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
            '<div class="note">Healed assets count as passing once: the gate did '
            "accept them, it just rewrote them on the way in.</div></div>")
    else:
        compliance_html = ('<div class="panel"><div class="empty">No verdicts in this '
                           "window yet. Post one with <code>asset-gatekeeper "
                           "&lt;folder&gt; --telemetry-url ...</code>.</div></div>")

    if growth_points:
        peak = max(point["peak_triangles"] for point in growth_points) or 1
        rows = []
        for point in growth_points:
            width = point["average_triangles"] / peak * 100
            rows.append(
                "<tr>"
                f'<td class="mono">{_e(point["bucket"])}</td>'
                f'<td>{point["assets"]:,}</td>'
                f'<td>{point["average_triangles"]:,}</td>'
                f'<td>{point["peak_triangles"]:,}</td>'
                f'<td>{point["total_triangles"]:,}</td>'
                f'<td style="width:28%"><div class="bar accent"><span '
                f'style="width:{width:.1f}%"></span></div></td>'
                "</tr>")
        growth_html = (
            '<div class="panel"><table><thead><tr><th>Bucket</th><th>Assets</th>'
            "<th>Average tris</th><th>Peak tris</th><th>Total tris</th>"
            "<th>Average vs peak</th></tr></thead><tbody>" + "".join(rows)
            + "</tbody></table>"
            '<div class="note">A rising average with a flat asset count is a content '
            "problem; a flat average with a rising count is a scope problem.</div></div>")
    else:
        note = growth.get("note", "no triangle counts in this window")
        growth_html = (f'<div class="panel"><div class="empty">{_e(note)}</div>'
                       '<div class="note">Growth needs the tris metric that every '
                       "mesh verdict carries.</div></div>")

    if clusters:
        rows = []
        for cluster in clusters:
            rows.append(
                "<tr>"
                f'<td>{cluster["count"]:,}</td>'
                f'<td class="mono">{_e(cluster["first_seen"][:10])} &rarr; '
                f'{_e(cluster["last_seen"][:10])}</td>'
                f'<td>{_e(cluster["example"])}</td>'
                "</tr>")
        errors_html = (
            '<div class="panel"><table><thead><tr><th style="width:80px">Count</th>'
            '<th style="width:190px">First seen &rarr; last seen</th>'
            "<th>Signature example</th></tr></thead><tbody>" + "".join(rows)
            + "</tbody></table>"
            '<div class="note">Grouped by signature: numbers are folded out, so the '
            "same mistake on fifty assets is one row.</div></div>")
    else:
        errors_html = ('<div class="panel"><div class="empty">No problems recorded '
                       "in this window.</div></div>")

    dashboard_href = "/dashboard" + (f"?project_id={project_id}" if project_id else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>AssetHealth - analytics</title>
<style>{ANALYTICS_CSS}</style></head>
<body><div class="wrap">
  <header>
    <h1>AssetHealth</h1>
    <span class="sub">analytics &middot; {scope}</span>
  </header>
  <nav>
    <a href="{dashboard_href}">&larr; Dashboard</a>
    <a href="/budget-planner{('?project_id=' + str(project_id)) if project_id else ''}">Budget planner &rarr;</a>
    <span class="chips">window</span> {window_links}
    <span class="chips">bucket</span> {bucket_links}
  </nav>

  <h2>Compliance</h2>
  <div class="cards">
    <div class="card"><div class="k">Compliance</div>
      <div class="v {compliance_class}">{compliance_text}</div></div>
    <div class="card"><div class="k">Verdicts</div>
      <div class="v accent">{summary.get('total', 0):,}</div></div>
    <div class="card"><div class="k">Failed</div>
      <div class="v bad">{summary.get('failed', 0):,}</div></div>
    <div class="card"><div class="k">Trend</div>
      <div class="v {"good" if delta >= 0 else "bad"}">{delta_text}</div></div>
  </div>
  {compliance_html}

  <h2>Polygon growth</h2>
  {growth_html}

  <h2>Error clusters</h2>
  {errors_html}

  <footer>Generated {_e(_utc_now())} &middot; GET
    <a href="/api/analytics?since={since}&amp;bucket={bucket}"
       style="color:#7aa2f7">/api/analytics</a> for the same numbers as JSON.</footer>
</div></body></html>"""



def render_dashboard(db_path: str | Path, project_id: int | None = None,
                     project_name: str | None = None, warn: str = "",
                     refresh: int = 0) -> str:
    stats = totals(db_path, project_id)
    shame = wall_of_shame(db_path, project_id=project_id)
    tops = bottlenecks(db_path, project_id=project_id)
    budget = budget_for_project(db_path, project_id)
    # Say it on the page, not just in the console: a demo pass rate that looks
    # like yours is the most expensive kind of confusion this tool can cause.
    demo_rows = demo_row_count(db_path)
    demo_notice = (
        f'<div class="notice err" style="display:block">'
        f'<strong>{demo_rows:,} verdicts on this page are demo data</strong> - the'
        f' bundled sample set, not your assets. Real runs are mixed in below.'
        f' Start clean with: <code>asset-health --db myproject.db</code>'
        f'</div>'
    ) if demo_rows else ""
    generated = _utc_now()
    scope = f"project: {_e(project_name)}" if project_name else "all projects"
    planner_href = "/budget-planner" + (f"?project_id={project_id}" if project_id else "")
    analytics_href = "/analytics" + (f"?project_id={project_id}" if project_id else "")

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
                   f"<td class=\"act\">{_send_button(entry)}</td>"
                "</tr>"
            )
        shame_html = (
            "<div class=\"panel\"><table><thead><tr>"
            "<th style=\"width:150px\">When</th><th style=\"width:290px\">Asset</th>"
            "<th>Why it failed</th><th style=\"width:170px\">Action</th></tr></thead><tbody>"
            + "".join(shame_rows)
            + "</tbody></table></div>"
        )
    else:
        shame_html = "<div class=\"panel\"><div class=\"empty\">Nothing has failed yet. Enjoy it.</div></div>"

    # Passed assets get the same hand-off: a clean gate is not the same thing as a
    # model that looks right, and reviewers keep asking to see the ones that
    # "passed" suspiciously cheaply.
    passed_rows = []
    for entry in recent_passed(db_path, 12, project_id):
        passed_rows.append(
            "<tr>"
            f"<td>{_e((entry.get('received_at') or '')[:16])}</td>"
            f"<td>{_e(entry.get('filename') or '')}</td>"
            f"<td class=\"act\">{_send_button(entry)}</td>"
            "</tr>")
    passed_html = (
        '<div class="panel"><h2>Recently passed - worth a look anyway</h2>'
        '<table><thead><tr><th style="width:150px">When</th><th>Asset</th>'
        '<th style="width:170px">Action</th></tr></thead><tbody>'
        + "".join(passed_rows) + "</tbody></table></div>") if passed_rows else ""

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
<title>AssetHealth - studio pipeline dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AssetHealth</h1>
    <span class="sub">studio pipeline dashboard &middot; {scope}{live_note}</span>
  </header>
  <div class="sub">Every number below comes from live verdicts posted by AssetGatekeeper.</div>
  {demo_notice}
  {f'<div class="notice err" style="display:block">{_e(warn)}</div>' if warn else ''}
  <nav>
    <a href="{analytics_href}">Analytics &rarr;</a>
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

  <h2>Wall of shame - last {min(len(shame), WALL_OF_SHAME_LIMIT)} failures</h2><span class='sub' style='margin-left:10px;font-weight:400'><a href='/audit/full'>all verdicts</a>  <a href='/audit/passes'>all passes</a></span>
  {shame_html}
{passed_html}

  <h2>Top bottlenecks</h2>
  {tops_html}

  <div id="live" class="sub" style="margin-top:14px"></div>
  <footer>
    <span>Generated {_e(generated)}</span>
    <span>Database <code>{_e(db_path)}</code></span>
    <span><code>POST /api/telemetry</code></span>
    <span><a href="/api/summary" style="color:#7aa2f7">/api/summary</a></span>
  </footer>
</div>
<script>
/* Live updates over Server-Sent Events. The page is a server-rendered snapshot,
 * so the cheapest correct reaction to "something changed" is a reload - but we
 * only reload when the tab is visible, and never more than once every 3 s, so a
 * 300-asset sweep cannot lock the browser in a refresh loop. */
(function () {{
  var live = document.getElementById("live");
  var token = new URLSearchParams(location.search).get("token") || "";
  var url = "/api/events" + (token ? "?token=" + encodeURIComponent(token) : "");
  if (!window.EventSource) {{ live.textContent = "live: unsupported browser"; return; }}
  var source = new EventSource(url);
  var pending = 0, timer = null;

  function mark(text) {{ live.textContent = text; }}

  function scheduleReload() {{
    if (timer) return;
    timer = setTimeout(function () {{
      timer = null;
      if (document.visibilityState === "visible") {{ location.reload(); }}
      else {{ pending = 0; }}          // hidden tab: drop it, it will reload when shown
    }}, 3000);
  }}

  source.addEventListener("verdict", function (event) {{
    var data = JSON.parse(event.data);
    pending += 1;
    mark("live  " + pending + " new since load  last: " + data.status + " " + (data.name || ""));
    scheduleReload();
  }});
  source.addEventListener("budget", function () {{
    mark("live  budget published, reloading");
    scheduleReload();
  }});
  source.onopen = function () {{ mark(pending ? live.textContent : "live  connected"); }};
  source.onerror = function () {{ mark("live  reconnecting"); }};
}})();
</script>
  <script src="/static/dashboard.js" defer></script>
</body>
</html>
"""


def render_error_page(status: int, headline: str, detail: str) -> str:
    """A refusal that explains itself, without leaking the project list."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>AssetHealth - {status}</title><style>{CSS}</style></head>
<body><div class="wrap">
  <header><h1>AssetHealth</h1><span class="sub">{status}</span></header>
  <div class="panel" style="margin-top:24px"><div class="empty" style="color:var(--bad)">
    <strong>{_e(headline)}</strong>
  </div></div>
  <div class="sub" style="margin-top:16px">{_e(detail)}</div>
</body></html>"""


def render_planner(db_path: str | Path, project_id: int, project_name: str,
                   token: str, projects: list[dict]) -> str:
    """The visual budget editor: sliders, a dropdown, checkboxes, one button."""
    budget = budget_for_project(db_path, project_id)
    options = "".join(
        f'<option value="{row["id"]}"{" selected" if row["id"] == project_id else ""}>'
        f'{_e(row["name"])}</option>' for row in projects)
    exceptions = list_exceptions(db_path, project_id)
    today = datetime.now(timezone.utc).date()

    def exception_row(item: dict) -> str:
        try:
            left = (date.fromisoformat(item["expires"]) - today).days
        except ValueError:
            left = 0
        state = ("expired" if left < 0 else f"ends in {left} day{'s' if left != 1 else ''}"
                 if left <= agk.EXCEPTION_WARN_DAYS else "active")
        tone = "err" if left < 0 else "warn" if left <= agk.EXCEPTION_WARN_DAYS else ""
        allow = item["allow"]
        shown = f"{allow:,}" if isinstance(allow, int) and not isinstance(allow, bool) else str(allow).lower()
        return (f'<tr><td><code>{_e(item["path"])}</code></td><td>{_e(item["rule"])} = {_e(shown)}</td>'
                f'<td>{_e(item["owner"])}</td><td>{_e(item["reason"])}</td>'
                f'<td class="{tone}">{_e(item["expires"])} &middot; {_e(state)}</td>'
                f'<td><button type="button" class="remove-exception" data-id="{int(item["id"])}">'
                f'Remove</button></td></tr>')

    exception_rows = "".join(exception_row(item) for item in exceptions) or (
        '<tr><td colspan="6" class="sub">None. Every asset gets the budget above.</td></tr>')
    rule_options = "".join(f'<option value="{name}">{name}</option>'
                           for name in sorted(agk.EXCEPTION_RULES))
    updated = budget.get("updated_at") or "never"
    updated_by = budget.get("updated_by") or "-"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AssetHealth - budget planner</title>
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

  <form class="form" id="budget-form" data-project="{project_id}" data--token="{_e(token)}">
    <div class="field">
      <label for="project">Project</label>
      <select id="project" disabled>{options}</select>
      <div class="hint">Switch project by opening this page with its own link.</div>
    </div>

    <div class="field">
      <label for="max_triangles">Max triangles</label>
      <div class="row">
        <input id="max_triangles" name="max_triangles" type="range"
               min="500" max="200000" step="500" value="{max(budget['max_triangles'], 500)}">
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
          {"checked" if budget["auto_heal"] else ""}> Enable auto-heal on every machine</label>
      </div>
      <div class="hint">Publishing this <strong>on</strong> turns folder-wide healing on for
        every artist and build machine that reads these rules: their gates will rename
        PNGs missing a suffix (repointing the .mtl/.gltf that name them), resize
        non-power-of-two PNGs and strip zero-area .obj faces, in their own folders. Each
        change is verified first and the original kept in <code>.agk-backups/</code>
        (undo: <code>asset-gatekeeper &lt;file&gt; --undo-heal</code>). Most teams leave it
        off and fix single assets with <code>asset-gatekeeper &lt;file&gt; --heal</code> or
        <strong>Check for fixes</strong> in the Art Workstation. CI pins it off with
        <code>--no-auto-heal</code>.</div>
    </div>

    <div>
      <button type="submit">Publish budgets</button>
      <div class="hint">Last published {_e(updated)} by {_e(updated_by)}</div>
    </div>
    <div class="notice" id="notice"></div>
  </form>

  <section class="form" id="exceptions" data-project="{project_id}" data--token="{_e(token)}">
    <h2>Exceptions</h2>
    <div class="hint">A named, dated waiver for some paths: "the boss may use 80,000
      triangles until alpha". Each needs a reason, an owner and an end date; the day
      after it ends, the asset fails again with "exception expired". Paths are globs
      under the watched folder (<code>*</code> crosses folders); a bare name matches
      that file anywhere.</div>
    <table class="exceptions">
      <thead><tr><th>Path</th><th>Allows</th><th>Owner</th><th>Reason</th><th>Ends</th><th></th></tr></thead>
      <tbody>{exception_rows}</tbody>
    </table>
    <div class="exception-form">
      <input id="ex-path" placeholder="Characters/Boss/*">
      <select id="ex-rule">{rule_options}</select>
      <input id="ex-allow" placeholder="80000 (or true/false)">
      <input id="ex-owner" placeholder="owner">
      <input id="ex-reason" placeholder="why">
      <input id="ex-expires" type="date">
      <button type="button" id="ex-add">Add exception</button>
    </div>
    <div class="notice" id="ex-notice"></div>
  </section>
</div>
<script>
(function () {{
  var box = document.getElementById("exceptions");
  var exNotice = document.getElementById("ex-notice");
  function send(path, payload) {{
    payload.project_id = Number(box.dataset.project);
    exNotice.className = "notice";
    exNotice.textContent = "Publishing...";
    return fetch(path, {{
      method: "POST",
      headers: {{"Content-Type": "application/json",
                "Authorization": "Bearer " + box.dataset.token}},
      body: JSON.stringify(payload)
    }}).then(function (response) {{
      return response.json().then(function (body) {{ return {{ok: response.ok, body: body}}; }});
    }}).then(function (result) {{
      if (result.ok) {{ window.location.reload(); return; }}
      exNotice.className = "notice err";
      exNotice.textContent = "Rejected: " + (result.body.error || "unknown error");
    }}).catch(function (error) {{
      exNotice.className = "notice err";
      exNotice.textContent = "Could not reach the server: " + error;
    }});
  }}
  document.getElementById("ex-add").addEventListener("click", function () {{
    var raw = document.getElementById("ex-allow").value.trim();
    var allow = raw === "true" ? true : raw === "false" ? false : Number(raw);
    send("/api/save-exception", {{exception: {{
      path: document.getElementById("ex-path").value.trim(),
      rule: document.getElementById("ex-rule").value,
      allow: allow,
      owner: document.getElementById("ex-owner").value.trim(),
      reason: document.getElementById("ex-reason").value.trim(),
      expires: document.getElementById("ex-expires").value
    }}}});
  }});
  Array.prototype.forEach.call(document.querySelectorAll(".remove-exception"), function (button) {{
    button.addEventListener("click", function () {{
      send("/api/remove-exception", {{id: Number(button.dataset.id)}});
    }});
  }});
}})();
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


# --- #
# HTTP
# --- #

class Handler(BaseHTTPRequestHandler):
    """Three routes, one auth check, no framework."""

    server_version = "AssetHealth/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"   # keep-alive; Content-Length is always set below

    # - plumbing - #

    def handle_error(self, request, client_address) -> None:
        """Stay quiet when a client simply went away mid-response.

        A browser that navigates during a response raises ConnectionReset here;
        printing a traceback for that made every page load look like a fault.
        """
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _drain_request_body(self) -> None:
        """Read and discard a form/JSON body we are about to refuse.

        An unconsumed body stays in the socket buffer, and the next keep-alive
        request on that connection starts by parsing it: a refused /admin/login
        whose body was ``GET /api/summary`` came back as a 200 with the smuggled
        request's data. Draining (or closing) is the whole fix.
        """
        if getattr(self, "_body_read", False) or self.command != "POST":
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > DRAIN_HARD_CAP:
            # Too big to swallow politely: end the connection instead. Bodies just
            # over the endpoint limit are still drained, so the client can read the
            # 413 it was promised instead of seeing a reset mid-upload.
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        if status >= 400:
            self._drain_request_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    # - auth - #

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
        _AUDIT.emit("auth.token.rejected",
                    actor=_presented_hint(presented),
                    action="authenticate",
                    outcome="denied",
                    remote=self.client_address[0],
                    detail={"path": urllib.parse.urlsplit(self.path).path,
                            "token_presented": bool(presented),
                            "admin_token_required": bool(admin)})
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

    # - routes - #

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_allowed():
            self._reject_host()
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/favicon.ico":
            # Every browser asks for this. Answering 204 costs one branch and
            # keeps the console clean, which is what the browser smoke test
            # asserts on.
            self._send(204, b"", "image/x-icon")
            return
        # The admin surface has its own auth domain (accounts, sessions, roles),
        # so it is routed before the studio--token identity is resolved.
        if path == "/admin" or path.startswith("/admin/") or path.startswith("/static/"):
            try:
                self._admin_get(path)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        try:
            identity = self._identify()
            if path == "/":
                query = urllib.parse.urlsplit(self.path).query
                location = "/dashboard" + (f"?{query}" if query else "")
                self._send(302, b"", "text/plain; charset=utf-8", {"Location": location})
            elif path == "/dashboard":
                self._dashboard(identity)
            elif path in ("/audit/full", "/audit/passes"):
                # Every verdict, paginated: the dashboard trims to a top-N, these
                # pages do not, so nothing is ever unreachable from the UI.
                self._audit_view(path, urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query))
            elif path == "/analytics":
                self._analytics_page(identity)
            elif path == "/budget-planner":
                self._planner(identity)
            elif path == "/api/summary":
                self._summary(identity)
            elif path == "/api/analytics":
                self._analytics_json(identity)
            elif path == "/api/rules":
                self._rules(identity)
            elif path == "/api/events":
                self._stream(identity)
            else:
                self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # a query error must not kill the handler thread
            self._json(500, {"error": "internal error", "detail": str(exc)})

    def _analytics_page(self, identity: dict) -> None:
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
        query = self._query()
        since = query.get("since", ["90d"])[0]
        bucket = query.get("bucket", ["week"])[0]
        if since not in {key for key, _ in ANALYTICS_WINDOWS}:
            since = "90d"
        if bucket not in ANALYTICS_BUCKETS:
            bucket = "week"
        name = ""
        if project_id is not None:
            match = [p for p in list_projects(self.server.db_path)
                     if p["id"] == project_id]
            if not match:
                self._forbid(404, "No such project",
                             f"project_id={project_id} is not registered")
                return
            name = match[0]["name"]
        try:
            body = render_analytics_page(self.server.db_path, project_id, name,
                                         since, bucket).encode("utf-8")
        except Exception as exc:                  # a trend query must not kill the page
            self._forbid(500, "Analytics unavailable", str(exc))
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _analytics_json(self, identity: dict) -> None:
        if identity["role"] == "denied":
            self._json(401, {"error": "unauthorised"})
            return
        project_id, error = self._scope(identity)
        if error in (400, 403):
            self._json(error, {"error": "bad project scope"})
            return
        if project_id is not None and not [p for p in list_projects(self.server.db_path)
                                           if p["id"] == project_id]:
            self._json(404, {"error": "no such project", "project_id": project_id})
            return
        query = self._query()
        since = query.get("since", ["90d"])[0]
        bucket = query.get("bucket", ["week"])[0]
        if since not in {key for key, _ in ANALYTICS_WINDOWS}:
            since = "90d"
        if bucket not in ANALYTICS_BUCKETS:
            bucket = "week"
        self._json(200, analytics_report(self.server.db_path, project_id, since, bucket))

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

    def _stream(self, identity: dict) -> None:
        """Server-Sent Events: push each new verdict to open dashboards.

        Deliberately a long-lived GET with ``Connection: close``: no chunked
        encoding to implement, every browser's EventSource reconnects on its own,
        and the only cost is one thread per open tab (daemon threads, so Ctrl+C
        is never held up).
        """
        if identity["role"] == "denied":
            self._json(401, {"error": "unauthorised"})
            return
        project_id, error = self._scope(identity)
        if error is not None:
            self._json(error, {"error": "bad project scope"})
            return

        server = self.server  # type: ignore[attr-defined]
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # nginx: do not buffer this
        self.end_headers()

        channel = server.broadcaster.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n\n: connected\n\n")
            self.wfile.flush()
            while not getattr(server, "stopping", False):
                try:
                    event = channel.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # keep proxies from closing
                    self.wfile.flush()
                    continue
                if event.get("project_id") not in (None, project_id) and project_id is not None:
                    continue
                payload = json.dumps(event, default=str)
                self.wfile.write(f"event: {event.get('type', 'message')}\n"
                                 f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass                                       # the tab was closed: normal
        finally:
            server.broadcaster.unsubscribe(channel)

    def _rules(self, identity: dict) -> None:
        """The published budget, in exactly the shape rules.toml uses.

        This is what `--remote-rules` points at, so a producer's click in the
        planner reaches every artist on their next run.

        Called *without* ``project_id`` it answers a different question - "what
        is this server's own rules.toml enforcing right now?" - and returns the
        read-only inspector snapshot. That is additive: the project-scoped shape
        below is unchanged, so an existing `--remote-rules` consumer sees
        exactly what it saw before.
        """
        if identity["role"] == "denied":
            self._json(401, {"error": "unauthorised"})
            return
        project_id, error = self._scope(identity)
        if error is not None:
            self._json(error, {"error": "bad project scope"})
            return
        if project_id is None:
            self._json(200, describe_rules(self.server))  # type: ignore[arg-type]
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        self._json(200, budget_document(budget_for_project(db_path, project_id),
                                        list_exceptions(db_path, project_id)))

    # - admin -
    # A second, cookie-based auth domain. Studio tokens scope a *pipeline*; the
    # admin page scopes a *person*, so it needs accounts, roles, sessions and an
    # audit trail - not a shared secret pasted into a CI variable.

    def _cookie(self, name: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _admin_user(self) -> dict | None:
        return session_user(self.server.db_path, self._cookie(SESSION_COOKIE))

    def _same_origin(self) -> bool:
        """Block cross-site form posts. Non-browser clients send no Origin."""
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        try:
            return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host", "")
        except ValueError:
            return False

    def _csrf_ok(self, user: dict) -> bool:
        token = self.headers.get("X-CSRF-Token", "")
        expected = user.get("csrf", "")
        return bool(token) and bool(expected) and hmac.compare_digest(token, expected)

    def _require_admin(self, writer: bool = False) -> dict | None:
        user = self._admin_user()
        if user is None:
            self._json(401, {"error": "sign in at /admin/login"})
            return None
        if writer and user["role"] not in WRITER_ROLES:
            self._json(403, {"error": f"role '{user['role']}' may read but not change "
                                      f"rules; ask a director or producer"})
            return None
        return user

    def _send_document(self, status: int, content_type: str, body: str | bytes,
                       extra: list[tuple[str, str]] | None = None) -> None:
        self._drain_request_body()
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, extra: list[tuple[str, str]] | None = None) -> None:
        # A 303 is still a response the client will not re-send a body for, so
        # the same drain applies: an unread form body becomes the next request.
        self._drain_request_body()
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()

    def _host_allowed(self) -> bool:
        """Does the Host header name this server, by a name we recognise?

        The policy lives in ``server.netguard`` because the workstation needs the
        same answer. Round 2 allowed only loopback and the bind address, which
        blacked out every studio that reaches the dashboard by LAN IP or by the
        machine's own name - the guard is about names an attacker controls, not
        about names we recognise.
        """
        return netguard.host_allowed(self.headers.get("Host", ""),
                                     self.server.server_address[0],
                                     getattr(self.server, "allow_hosts", ()))

    def _reject_host(self) -> None:
        self._json(403, {
            "error": "unexpected Host header",
            "detail": netguard.refusal_detail(self.headers.get("Host", "")),
            "host": self.headers.get("Host", ""),
        })

    def _active_project(self) -> Path | None:
        """The project the picker selected, if it still exists."""
        try:
            with open_db(self.server.db_path) as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'project_root'").fetchone()
        except STORAGE_ERRORS:
            return None
        if not row or not str(row[0] or "").strip():
            return None
        path = Path(row[0])
        return path if path.is_dir() else None

    def _rules_path(self) -> Path:
        """rules.toml for the active project, else the configured location."""
        project = self._active_project()
        if project is not None:
            return project / agk.RULES_FILENAME
        server = self.server
        if server.rules_path:
            return Path(server.rules_path)
        return Path(server.rules_root) / agk.RULES_FILENAME

    def _scan_target(self) -> Path:
        project = self._active_project()
        return project if project is not None else Path(self.server.scan_root)

    def _allowed_path(self, raw: str) -> Path | None:
        """Resolve a user-supplied folder, but only inside an allowed root.

        ``..`` in a path is exactly what this check exists for, so the comparison
        is made on fully resolved paths, never on the string the client sent.
        """
        text = str(raw or "").strip().strip('"')
        if not text:
            return None
        try:
            candidate = Path(text).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        for root in self.server.allow_roots:
            if candidate == root or root in candidate.parents:
                return candidate
        return None

    def _serve_admin_static(self, name: str) -> None:
        if name not in ADMIN_STATIC:
            self._json(404, {"error": "not found"})
            return
        path = STATIC_DIR / name
        try:
            payload = path.read_bytes()
        except OSError:
            self._json(404, {"error": "not installed"})
            return
        suffix = path.suffix.lower()
        kind = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript',
                '.css': 'text/css'}.get(suffix, 'application/octet-stream')
        self._send_document(200, kind, payload)

    def _admin_get(self, path: str) -> None:
        if path.startswith("/static/"):
            self._serve_admin_static(path[len("/static/"):])
            return
        if path == "/admin/login":
            if self._admin_user() is not None:
                self._redirect("/admin")
                return
            error = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            message = (error.get("error") or [""])[0]
            username = (error.get("u") or [""])[0]
            self._send_document(200, "text/html; charset=utf-8",
                                render_login_page(message, username,
                                                  first_run=admin_account_count(
                                                      self.server.db_path) == 0))
            return
        user = self._admin_user()
        if path in ("/admin", "/admin/index.html"):
            if user is None:
                self._redirect("/admin/login")
                return
            self._serve_admin_static("admin.html")
            return
        if path == "/admin/api/state":
            if self._require_admin() is None:
                return
            server = self.server
            rules_path = self._rules_path()
            error = ""
            try:
                document = agk.parse_rules(rules_path) if rules_path.is_file() else {}
            except agk.ConfigError as exc:
                document, error = {}, str(exc)
            project = self._active_project()
            self._json(200, {
                "user": {"username": user["username"], "role": user["role"]},
                "csrf": user["csrf"],
                "can_write": user["role"] in WRITER_ROLES,
                "rules_path": str(rules_path),
                "rules_exists": rules_path.is_file(),
                "rules_error": error,
                "project": str(project) if project else "",
                "allowed_roots": [str(root) for root in server.allow_roots],
                "recent_projects": recent_projects(server.db_path),
                "scan_root": str(self._scan_target()),
                "document": document,
                "presets": agk.preset_summary(),
                "audit": list_audit(server.db_path, 25),
                "scan": server.scanner.snapshot(),
            })
            return
        if path == "/admin/api/audit":
            if self._require_admin() is None:
                return
            self._json(200, {"audit": list_audit(self.server.db_path, 200)})
            return
        if path == "/admin/api/projects":
            if self._require_admin() is None:
                return
            project = self._active_project()
            self._json(200, {
                "active": str(project) if project else "",
                "allowed_roots": [str(root) for root in self.server.allow_roots],
                "recent": recent_projects(self.server.db_path),
            })
            return
        if path == "/admin/api/scan/events":
            self._admin_events()
            return
        self._json(404, {"error": "not found", "path": path})

    def _admin_events(self) -> None:
        if self._require_admin() is None:
            return
        server = self.server
        channel = server.scan_broadcaster.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for event in server.scanner.recent():
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while not server.stopping:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            server.scan_broadcaster.unsubscribe(channel)

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _login_throttled(self) -> bool:
        server = self.server
        now = time.time()
        with server._login_lock:
            failures = [stamp for stamp in server._login_failures.get(self._client_ip(), [])
                        if now - stamp < LOGIN_WINDOW]
            server._login_failures[self._client_ip()] = failures
            return len(failures) >= LOGIN_MAX_FAILURES

    def _note_login_failure(self) -> None:
        server = self.server
        with server._login_lock:
            server._login_failures.setdefault(self._client_ip(), []).append(time.time())

    def _is_loopback(self) -> bool:
        address = (self.client_address[0] if self.client_address else "") or ""
        return address in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _admin_bootstrap(self) -> None:
        """Create the first director account from the browser.

        A zero-CLI install cannot require a terminal for its first step, so an
        empty user table turns the sign-in page into a setup page. Three guards:
        it only works while there are no accounts, only from loopback, and it
        always creates a *director* - whoever sets up the box is accountable.
        """
        if not self._same_origin():
            self._json(403, {"error": "cross-origin request refused"})
            return
        if admin_account_count(self.server.db_path) > 0:
            self._json(403, {"error": "an account already exists; sign in instead"})
            return
        if not self._is_loopback():
            self._json(403, {"error": "first-run setup is only allowed from this machine",
                             "detail": "open http://127.0.0.1:<port>/admin locally"})
            return
        if self._login_throttled():
            self._json(429, {"error": "too many attempts; wait a few minutes"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"error": "missing form data"})
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        self._body_read = True
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        confirm = (form.get("confirm") or [""])[0]
        if not username or len(username) > 64:
            self._redirect("/admin/login?error=Choose+a+username")
            return
        if len(password) < 10:
            self._note_login_failure()
            self._redirect("/admin/login?error=Use+at+least+10+characters&u="
                           + urllib.parse.quote(username))
            return
        if password != confirm:
            self._note_login_failure()
            self._redirect("/admin/login?error=Passwords+do+not+match&u="
                           + urllib.parse.quote(username))
            return
        try:
            create_admin_user(self.server.db_path, username, "director", password,
                              created_by="first-run setup")
        except (ValueError, sqlite3.IntegrityError) as exc:
            self._json(400, {"error": str(exc)})
            return
        record_audit(self.server.db_path, {"id": None, "username": username,
                                          "role": "director"},
                     "account", "", f"{username} (director)", self._client_ip(),
                     summary="first-run setup created the first director")
        with open_db(self.server.db_path) as connection:
            row = connection.execute(
                "SELECT id, role FROM admin_users WHERE username = ?", (username,)).fetchone()
        token, _csrf = create_session(self.server.db_path, int(row["id"]), self._client_ip())
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={int(SESSION_TTL)}")
        self._redirect("/admin", [("Set-Cookie", cookie)])

    def _admin_login(self) -> None:
        if not self._same_origin():
            self._json(403, {"error": "cross-origin sign-in refused"})
            return
        if self._login_throttled():
            self._json(429, {"error": "too many failed attempts; wait a few minutes"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._redirect("/admin/login?error=Missing+credentials")
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        self._body_read = True
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        db_path = self.server.db_path
        row = None
        with open_db(db_path) as connection:
            row = connection.execute(
                "SELECT id, password_hash, role FROM admin_users "
                " WHERE username = ? AND disabled = 0", (username,)).fetchone()
        # Always run a hash comparison: a fast path for unknown users leaks which
        # usernames exist.
        reference = row["password_hash"] if row else hash_password("never-matches")
        if not verify_password(password, reference) or row is None:
            self._note_login_failure()
            self._redirect("/admin/login?error=Invalid+credentials&u="
                           + urllib.parse.quote(username))
            return
        token, _csrf = create_session(db_path, int(row["id"]), self._client_ip())
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={int(SESSION_TTL)}")
        self._redirect("/admin", [("Set-Cookie", cookie)])

    def _admin_apply(self) -> None:
        user = self._require_admin(writer=True)
        if user is None:
            return
        if not self._same_origin():
            self._json(403, {"error": "cross-origin request refused"})
            return
        if not self._csrf_ok(user):
            self._json(403, {"error": "missing or stale CSRF token; reload the page"})
            return
        payload = self._read_json()
        if payload is None:
            return
        submitted = payload.get("document")
        if not isinstance(submitted, dict):
            self._json(400, {"error": "document must be an object"})
            return
        rules_path = self._rules_path()
        try:
            existing = agk.parse_rules(rules_path) if rules_path.is_file() else {}
        except agk.ConfigError as exc:
            self._json(409, {"error": f"the current rules.toml does not parse: {exc}",
                             "detail": "fix it by hand once; the admin page will not "
                                       "overwrite a file it cannot read"})
            return
        merged = merge_rules_document(existing, submitted)
        # A `platform` name with no matching [platforms.<name>] table makes the gate
        # refuse the whole file, so such a reference can never be "kept for later".
        # Dropping it is a repair, and repairs are reported, never silent.
        notes: list[str] = []
        platform = str(merged.get("platform") or "").strip()
        if platform and platform not in (merged.get("platforms") or {}):
            notes.append(f"dropped platform '{platform}': there is no "
                         f"[platforms.{platform}] table, so the gate would refuse "
                         f"this file. The preset's numbers are already in [mesh].")
            merged.pop("platform", None)
        try:
            validate_rules_document(merged)
        except agk.ConfigError as exc:
            self._json(400, {"error": f"refused: {exc}"})
            return
        changes = diff_documents(existing, merged)
        try:
            write_rules_atomic(rules_path, merged)
        except OSError as exc:
            self._json(500, {"error": f"cannot write {rules_path}: {exc}"})
            return
        # The audit row names the file as well as the key: with more than one
        # project open on the same dashboard, "mesh.max_triangles" alone is not
        # enough to know which rules.toml moved.
        note = str(payload.get("summary") or "").strip()
        summary = f"{note} @ {rules_path}" if note else str(rules_path)
        for key, old_value, new_value in changes:
            record_audit(self.server.db_path, user, key, old_value, new_value,
                         self._client_ip(), summary=summary)
        started = False
        if payload.get("action") == "rescan":
            started = self.server.scanner.start(
                str(self._scan_target()), rules_path,
                f"http://127.0.0.1:{self.server.server_address[1]}/api/telemetry")
        self._json(200, {
            "ok": True,
            "path": str(rules_path),
            "notes": notes,
            "changed": [{"key": k, "old": o, "new": n} for k, o, n in changes],
            "scan": {"started": started, **self.server.scanner.snapshot()},
        })

    def _admin_scan(self) -> None:
        user = self._require_admin()
        if user is None:
            return
        if not self._same_origin() or not self._csrf_ok(user):
            self._json(403, {"error": "missing or stale CSRF token; reload the page"})
            return
        started = self.server.scanner.start(
            str(self._scan_target()), self._rules_path(),
            f"http://127.0.0.1:{self.server.server_address[1]}/api/telemetry")
        if not started:
            self._json(409, {"error": "a scan is already running",
                             **self.server.scanner.snapshot()})
            return
        self._json(200, {"ok": True, "scan": self.server.scanner.snapshot()})

    def _admin_project(self) -> None:
        """Point this dashboard at a project folder (the whole zero-CLI picker)."""
        user = self._require_admin(writer=True)
        if user is None:
            return
        if not self._same_origin():
            self._json(403, {"error": "cross-origin request refused"})
            return
        if not self._csrf_ok(user):
            self._json(403, {"error": "missing or stale CSRF token; reload the page"})
            return
        payload = self._read_json()
        if payload is None:
            return
        raw = str(payload.get("path") or "")
        target = self._allowed_path(raw)
        if target is None:
            self._json(403, {
                "error": "that folder is outside the allowed roots",
                "detail": f"allowed: {', '.join(str(root) for root in self.server.allow_roots)}",
                "path": raw,
            })
            return
        if not target.is_dir():
            self._json(400, {"error": f"not a folder: {target}"})
            return
        previous = self._active_project()
        with _WRITE_LOCK, open_db(self.server.db_path) as connection:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                               "('project_root', ?)", (str(target),))
            connection.execute("INSERT OR REPLACE INTO project_roots (path, label, last_used)"
                               " VALUES (?, ?, ?)", (str(target), target.name, _utc_now()))
            connection.execute("DELETE FROM project_roots WHERE path NOT IN "
                               "(SELECT path FROM project_roots ORDER BY last_used DESC LIMIT 20)")
        # Only a real switch is an event. Re-selecting the same folder wrote a
        # no-op row (old == new), which is noise in a table people audit.
        if previous != target:
            record_audit(self.server.db_path, user, "project",
                         str(previous) if previous else "", str(target), self._client_ip(),
                         summary="project folder selected")
        rules_path = target / agk.RULES_FILENAME
        error = ""
        document: dict = {}
        try:
            if rules_path.is_file():
                document = agk.parse_rules(rules_path)
        except agk.ConfigError as exc:
            error = str(exc)
        self._json(200, {"ok": True, "project": str(target),
                         "rules_path": str(rules_path),
                         "rules_exists": rules_path.is_file(),
                         "rules_error": error, "document": document,
                         "recent": recent_projects(self.server.db_path)})

    def _admin_post(self, path: str) -> None:
        if path == "/admin/api/project":
            self._admin_project()
        elif path == "/admin/api/bootstrap":
            self._admin_bootstrap()
        elif path == "/admin/login":
            self._admin_login()
        elif path == "/admin/api/logout":
            user = self._require_admin()
            if user is None:
                return
            if self._csrf_ok(user):
                delete_session(self.server.db_path, self._cookie(SESSION_COOKIE))
            self._redirect("/admin/login", [("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; "
                                                           f"Max-Age=0; SameSite=Strict")])
        elif path == "/admin/api/apply":
            self._admin_apply()
        elif path == "/admin/api/scan":
            self._admin_scan()
        else:
            self._json(404, {"error": "not found", "path": path})

    def _configured_tags(self) -> list[str]:
        try:
            rules = self._rules_path()
            document = agk.parse_rules(rules) if rules.is_file() else {}
            return sorted((document.get("tags") or {}).keys())
        except (agk.ConfigError, OSError):
            return []

    def _audit_view(self, path: str, query: dict) -> None:
        """Every verdict, paginated - no top-N slicing, no hidden rows."""
        identity = self._identify()
        if identity.get("role") == "denied":
            self._json(401, {"error": "a token is required to view the dashboard"})
            return
        default = "pass" if path == "/audit/passes" else "all"
        status = (query.get("status", [default])[0] or default).lower()
        if status not in ("all", "pass", "fail"):
            status = "all"
        if path == "/audit/passes":
            status = "pass"
        selected = [item for item in query.get("tag", []) if item.strip()]
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
        except (TypeError, ValueError):
            page = 1
        tag_names = self._configured_tags()
        data = audit_page(self.server.db_path, self.server.scan_root, tag_names,
                          () if status == "all" else (status,), selected, page)
        title = "Passes" if path == "/audit/passes" else "All verdicts"
        body = render_audit_page(data, title=title, status_filter=status,
                                 tag_names=tag_names, selected=selected,
                                 base_path=path, reviews=self._workstation_reviews())
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
    # - workstation bridge
    def _workstation_reviews(self) -> dict:
        """Sign-off status per file from the Art Workstation, best effort (W2).

        One short request per page; a workstation that is down or slow just
        means no review column, never a slow or broken audit page.
        """
        base = self._workstation_url()
        if not base:
            return {}
        request = urllib.request.Request(f"{base}/api/assets")
        if self.server.workstation_token:
            request.add_header("Authorization", f"Bearer {self.server.workstation_token}")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                assets = json.loads(response.read() or b"{}").get("assets", [])
        except (OSError, ValueError, urllib.error.URLError):
            return {}
        found: dict = {}
        for asset in assets:
            status = asset.get("review_status") or ""
            if not status:
                continue
            path = str(asset.get("source_path") or "").replace("\\", "/").lower()
            if path:
                found[("path", path)] = status
            found.setdefault(("name", str(asset.get("filename") or "").lower()), status)
        return found

    def _workstation_url(self) -> str:
        """Where to POST an asset: explicit flag, else the running one.

        The launcher writes .agk/ports.json, so the dashboard can find the
        workstation it was started beside without being told the port up front
        (it may have moved for port fallback, and then the flag would be stale).
        """
        explicit = (getattr(self.server, "workstation_url", "") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        candidates = []
        ports = os.environ.get(PORTS_FILE_ENV, "")
        if ports:
            candidates.append(Path(ports))
        candidates.append(Path.cwd() / ".agk" / "ports.json")
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            url = ((data.get("workstation") or {}).get("url") or "").strip()
            if url:
                return url.rstrip("/")
        return ""

    def _asset_tags(self, source: Path) -> list[str]:
        """The live categories this file falls into, in the gate's own order."""
        try:
            rules = self._rules_path()
            document = agk.parse_rules(rules) if rules.is_file() else {}
            known = {agk.normalise_tag(name): name for name in (document.get("tags") or {})}
        except (agk.ConfigError, OSError):
            return []
        if not known:
            return []
        matched: list[str] = []
        for candidate in agk.tag_candidates(source, Path(self.server.scan_root)):
            name = known.get(agk.normalise_tag(candidate))
            if name and name not in matched:
                matched.append(name)
        return matched[:3]

    def _send_sidecars(self, base: str, source: Path, asset_id) -> tuple[list, list]:
        """Send the files ``source`` references (a .bin, a .mtl, textures).

        The bridge used to post the one file, so a .gltf the gate had PASSED
        arrived at the workstation unreadable ("could not read mesh: rock.bin").
        Only files that resolve next to the asset are sent, under the relative
        path the asset uses; each is size-capped like an upload.
        """
        if not isinstance(asset_id, int):
            return [], []
        sent, errors = [], []
        try:
            references = agk.resolve_references(source)
        except Exception as exc:                       # the main file is already there
            return [], [f"could not list references: {exc}"]
        seen = set()
        for written, target in references[:64]:
            if written in seen or not target.is_file():
                continue
            seen.add(written)
            try:
                size = target.stat().st_size
                if size > 64 * 1024 * 1024:
                    errors.append(f"{written}: larger than 64 MB, not sent")
                    continue
                query = urllib.parse.urlencode({"filename": written})
                request = urllib.request.Request(f"{base}/api/assets/{asset_id}/files?{query}",
                                                 data=target.read_bytes(), method="POST")
                if self.server.workstation_token:
                    request.add_header("Authorization", f"Bearer {self.server.workstation_token}")
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
                sent.append(written)
            except urllib.error.HTTPError as exc:
                errors.append(f"{written}: the workstation said {exc.code}")
            except OSError as exc:
                errors.append(f"{written}: {exc}")
        return sent, errors

    def _send_to_workstation(self, identity: dict) -> None:
        if identity.get("role") == "denied":
            self._json(401, {"error": "a token is required to use this dashboard"})
            return
        payload = self._read_json()
        if payload is None:
            return
        ids = payload.get("verdicts")
        if not isinstance(ids, list) or not ids:
            self._json(400, {"error": "send a list of verdict ids"})
            return
        if len(ids) > MAX_SEND_BATCH:
            self._json(400, {"error": f"at most {MAX_SEND_BATCH} assets at a time "
                                      f"(asked for {len(ids)})"})
            return
        base = self._workstation_url()
        if not base:
            self._json(400, {"error": "no Art Workstation to send to: start it "
                                      "(agk-serve.bat up) or pass --workstation-url"})
            return
        results = []
        for raw in ids[:MAX_SEND_BATCH]:
            try:
                verdict_id = int(raw)
            except (TypeError, ValueError):
                results.append({"ok": False, "id": raw, "error": "not a verdict id"})
                continue
            with open_db(self.server.db_path) as connection:
                row = connection.execute(
                    "SELECT id, filename, filepath, passed FROM verdicts WHERE id = ?",
                    (verdict_id,)).fetchone()
                reasons = [item[0] for item in connection.execute(
                    "SELECT message FROM problems WHERE verdict_id = ? ORDER BY id",
                    (verdict_id,))]
            if row is None:
                results.append({"ok": False, "id": verdict_id, "error": "unknown verdict"})
                continue
            source = Path(row["filepath"] or "")
            if not source.is_file():
                results.append({"ok": False, "id": verdict_id, "name": row["filename"],
                                "error": f"that file is not reachable here: {source}"})
                continue
            tags = self._asset_tags(source)
            query = urllib.parse.urlencode({
                "filename": source.name,
                "verdict": "passed" if row["passed"] else "failed",
                "problems": "\n".join(reasons),
                "tags": ", ".join(tags),
                "source": "assethealth",
                "verdict_id": verdict_id,
                # Where the file came from (W8): the path the gate reported.
                "source_path": str(row["filepath"] or ""),
            })
            request = urllib.request.Request(f"{base}/api/assets?{query}",
                                             data=source.read_bytes(), method="POST")
            if self.server.workstation_token:
                request.add_header("Authorization", f"Bearer {self.server.workstation_token}")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = json.loads(response.read() or b"{}")
                sidecars, sidecar_errors = self._send_sidecars(base, source, body.get("id"))
                results.append({"ok": True, "id": verdict_id, "name": source.name,
                                "asset_id": body.get("id"), "tags": tags,
                                "sidecars": sidecars, "sidecar_errors": sidecar_errors})
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:160].decode("utf-8", "replace")
                results.append({"ok": False, "id": verdict_id, "name": source.name,
                                "error": f"the workstation said {exc.code}: {detail}"})
            except OSError as exc:
                results.append({"ok": False, "id": verdict_id, "name": source.name,
                                "error": str(exc)})
        sent = sum(1 for item in results if item["ok"])
        self._json(200, {"requested": len(ids), "sent": sent, "limit": MAX_SEND_BATCH,
                         "results": results})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self._host_allowed():
            # Before anything is read or created: this is the request shape a
            # rebinding attack uses to reach a loopback-only service.
            self._reject_host()
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path.startswith("/admin/"):
            self._admin_post(path)
            return
        if path == "/api/send-to-workstation":
            self._send_to_workstation(self._identify())
            return
        if path not in ("/api/telemetry", "/api/save-budget", "/api/save-exception",
                        "/api/remove-exception"):
            self._json(404, {"error": "not found", "path": path})
            return
        payload = self._read_json()
        if payload is None:      # _read_json already answered the request
            return
        if path == "/api/telemetry":
            self._ingest(payload)
        elif path == "/api/save-budget":
            self._save_budget(payload)
        else:
            self._save_exception(payload, remove=path == "/api/remove-exception")

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
        self._body_read = True
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
        except STORAGE_ERRORS as exc:
            self._json(500, {"error": "database error", "detail": str(exc)})
            return

        problems = payload.get("problems") or []
        if problems and not payload.get("passed"):
            _AUDIT.emit("budget.violation",
                        actor=str(payload.get("username") or "unknown"),
                        action="validate",
                        outcome="failed",
                        project_id=project_id,
                        remote=self.client_address[0],
                        detail={"asset": payload.get("name") or payload.get("filename"),
                                "problem_count": len(problems),
                                "first_problem": str(problems[0])})
        self._maybe_alert(payload, project_id)
        broadcaster = getattr(self.server, "broadcaster", None)  # type: ignore[attr-defined]
        if broadcaster is not None:
            broadcaster.publish({
                "type": "verdict",
                "id": verdict_id,
                "project_id": project_id,
                "name": payload.get("name") or payload.get("filename"),
                "passed": bool(payload.get("passed")),
                "status": payload.get("status") or ("PASSED" if payload.get("passed") else "FAILED"),
                "metric": payload.get("metric", ""),
            })
        self._json(201, {"stored": True, "id": verdict_id, "project_id": project_id})

    def _maybe_alert(self, payload: dict, project_id: int | None) -> None:
        """Push a webhook when a failure blows a budget by more than 50%.

        Throttled per scope so a nightly sweep failing 300 assets sends one
        message, not 300. The asset still lands in the database either way - the
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
        if getattr(server, "digest_at", None):
            return      # a daily digest replaces one message per failure
        person = person_for(getattr(server, "people", ()), payload.get("username", ""),
                            payload.get("hostname", ""))
        # Per person: one artist's broken batch must not silence another's alert.
        scope = alert_scope(project_id) + (f":person:{person.key}" if person else "")
        if not claim_alert_slot(server.db_path, scope, server.alert_cooldown):
            server.alerts_suppressed += 1
            if not getattr(server, "quiet", False):
                print(f"asset_health: alert for {scope} suppressed by the "
                      f"{server.alert_cooldown:g}s cooldown", file=sys.stderr)
            return
        text = alert_text(payload, budget, overshoot)
        if person is not None:
            text = f"{mention_for(notifier.provider, person)} {text}"
        link = workstation_link(server.workstation_url, payload.get("path", "") or "",
                                payload.get("name") or payload.get("filename") or "")
        if link:
            text += f" Open it: {link}"
        notifier.submit(text, person)

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
        broadcaster = getattr(self.server, "broadcaster", None)  # type: ignore[attr-defined]
        if broadcaster is not None:
            broadcaster.publish({"type": "budget", "project_id": requested})
        self._json(200, {"saved": True, "project_id": requested,
                         "budget": {k: v for k, v in saved.items()
                                    if k not in ("updated_at", "updated_by")}})

    def _writable_project(self, payload: dict) -> int | None:
        """The project a token may publish to, or None after answering 4xx."""
        identity = self._identify()
        if identity["role"] in ("denied", "open"):
            self._json(401 if identity["role"] == "denied" else 403,
                       {"error": "a project or admin token is required to publish budgets"})
            return None
        requested = payload.get("project_id")
        if identity["role"] == "project":
            requested = identity["project_id"]
        if not isinstance(requested, int) or isinstance(requested, bool):
            self._json(400, {"error": "project_id is required"})
            return None
        if not any(row["id"] == requested for row in list_projects(self.server.db_path)):
            self._json(404, {"error": f"no such project: {requested}"})
            return None
        return requested

    def _save_exception(self, payload: dict, remove: bool) -> None:
        project_id = self._writable_project(payload)
        if project_id is None:
            return
        db_path = self.server.db_path  # type: ignore[attr-defined]
        if remove:
            exception_id = payload.get("id")
            if not isinstance(exception_id, int) or isinstance(exception_id, bool):
                self._json(400, {"error": "id is required"})
                return
            if not remove_exception(db_path, project_id, exception_id):
                self._json(404, {"error": f"no exception {exception_id} in this project"})
                return
            result = {"removed": exception_id}
        else:
            try:
                result = {"saved": add_exception(db_path, project_id, payload.get("exception"),
                                                 created_by=payload.get("updated_by", "") or "")}
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
        broadcaster = getattr(self.server, "broadcaster", None)  # type: ignore[attr-defined]
        if broadcaster is not None:
            broadcaster.publish({"type": "budget", "project_id": project_id})
        self._json(200, {**result, "project_id": project_id,
                         "exceptions": list_exceptions(db_path, project_id)})

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


# --- #
# Admin: accounts, sessions, audit trail, rules writes
# --- #

PBKDF2_ROUNDS = 120_000
ADMIN_ROLES = ("director", "producer", "artist")
WRITER_ROLES = frozenset({"director", "producer"})
SESSION_COOKIE = "agk_admin"
SESSION_TTL = 12 * 3600.0
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 300.0
STATIC_DIR = HERE / "static"
ADMIN_STATIC = frozenset({"admin.html", "admin.js", "admin.css", "dashboard.js"})


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(text.encode("ascii"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 with a per-user salt, versioned for future rounds.

    Standard library only, and never reversible: the database holds no password,
    only this string. ``agk_admin`` passwords are studio credentials, so they get
    the same treatment as anything else that must not appear in a backup.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored ``pbkdf2_sha256$rounds$salt$hash``."""
    try:
        algorithm, rounds, salt, digest = str(stored).split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                        _unb64(salt), int(rounds))
        expected = _unb64(digest)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def create_admin_user(db_path: str | Path, username: str, role: str,
                      password: str | None = None, created_by: str = "") -> str:
    """Create an admin account. Returns the password (generated when None)."""
    username = str(username or "").strip()
    if not username:
        raise ValueError("username is required")
    if role not in ADMIN_ROLES:
        raise ValueError(f"role must be one of {', '.join(ADMIN_ROLES)}")
    password = password or secrets.token_urlsafe(18)
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO admin_users (username, role, password_hash, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, role, hash_password(password), _utc_now(), created_by))
    return password


def list_admin_users(db_path: str | Path) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT id, username, role, created_at, disabled FROM admin_users ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def set_admin_password(db_path: str | Path, username: str,
                       password: str | None = None) -> str:
    password = password or secrets.token_urlsafe(18)
    with _WRITE_LOCK, open_db(db_path) as connection:
        cursor = connection.execute("UPDATE admin_users SET password_hash = ? WHERE username = ?",
                                    (hash_password(password), username))
        if cursor.rowcount == 0:
            raise ValueError(f"no such user: {username}")
        # Resetting a password invalidates every open session for that account.
        connection.execute("DELETE FROM admin_sessions WHERE user_id IN "
                           "(SELECT id FROM admin_users WHERE username = ?)", (username,))
    return password


def admin_account_count(db_path: str | Path) -> int:
    try:
        with open_db(db_path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])
    except STORAGE_ERRORS:
        return 0


def create_session(db_path: str | Path, user_id: int, ip: str = "") -> tuple[str, str]:
    """Open a session. Returns (cookie token, CSRF token); only hashes are stored."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO admin_sessions (token_hash, user_id, created_at, expires_at, ip, csrf)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_sha256(token), user_id, _utc_now(), time.time() + SESSION_TTL, ip, csrf))
        connection.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (time.time(),))
    return token, csrf


def session_user(db_path: str | Path, token: str) -> dict | None:
    """The signed-in user for this cookie token, or None when it is stale."""
    if not token:
        return None
    with open_db(db_path) as connection:
        row = connection.execute(
            "SELECT u.id, u.username, u.role, s.csrf, s.expires_at"
            "  FROM admin_sessions s JOIN admin_users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND u.disabled = 0", (_sha256(token),)).fetchone()
    if row is None or float(row["expires_at"]) < time.time():
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "csrf": row["csrf"]}


def delete_session(db_path: str | Path, token: str) -> None:
    if not token:
        return
    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_sha256(token),))


def record_audit(db_path: str | Path, user: dict, target: str, old_value: object,
                 new_value: object, ip: str = "", summary: str = "") -> None:
    """One row per changed key: who, when, where, and both sides of the change."""
    def show(value: object) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, sort_keys=True, default=str)

    with _WRITE_LOCK, open_db(db_path) as connection:
        connection.execute(
            "INSERT INTO rule_audit (changed_at, user_id, username, role, target,"
            " summary, old_value, new_value, source_ip) VALUES (?,?,?,?,?,?,?,?,?)",
            (_utc_now(), user.get("id"), user.get("username", "?"), user.get("role", ""),
             target, summary, show(old_value), show(new_value), ip))


def list_audit(db_path: str | Path, limit: int = 100) -> list[dict]:
    with open_db(db_path) as connection:
        rows = connection.execute(
            "SELECT changed_at, username, role, target, summary, old_value, new_value"
            "  FROM rule_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(row) for row in rows]


#: The bridge is for reviewing one asset at a time. Ten is the ceiling enforced
#: here (not just in the page) so a stuck button cannot queue the workstation.
MAX_SEND_BATCH = 10
PORTS_FILE_ENV = "AGK_PORTS_FILE"


def recent_passed(db_path: str | Path, limit: int = 12,
                  project_id: int | None = None) -> list[dict]:
    """Passed assets are worth reviewing too - a clean gate does not mean it looks
    right - so the dashboard offers the same one-click hand-off for them."""
    where = "WHERE passed = 1" + (" AND project_id = ?" if project_id is not None else "")
    params: list[object] = [limit] if project_id is None else [int(project_id), limit]
    try:
        with open_db(db_path) as connection:
            rows = connection.execute(
                f"SELECT id, filename, filepath, hostname, received_at FROM verdicts "
                f"{where} ORDER BY id DESC LIMIT ?", params).fetchall()
    except STORAGE_ERRORS:
        return []
    return [dict(row) for row in rows]


def recent_projects(db_path: str | Path, limit: int = 10) -> list[dict]:
    """Folders the picker has been pointed at, newest first."""
    try:
        with open_db(db_path) as connection:
            rows = connection.execute(
                "SELECT path, label, last_used FROM project_roots"
                " ORDER BY last_used DESC LIMIT ?", (int(limit),)).fetchall()
    except STORAGE_ERRORS:
        return []
    return [dict(row) for row in rows]


def flatten_document(value: object, prefix: str = "") -> dict[str, object]:
    """Dotted paths for every leaf, so an audit row can name exactly what moved."""
    flat: dict[str, object] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(flatten_document(item, f"{prefix}.{key}" if prefix else str(key)))
    else:
        flat[prefix] = value
    return flat


def diff_documents(old: dict, new: dict) -> list[tuple[str, object, object]]:
    before = flatten_document(old)
    after = flatten_document(new)
    changes: list[tuple[str, object, object]] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key, None) != after.get(key, None):
            changes.append((key, before.get(key), after.get(key)))
    return changes


def merge_rules_document(existing: dict, submitted: dict) -> dict:
    """Overlay what the form owns, keep every section it does not show.

    The admin page edits budgets, texture policy and categories. [filters],
    [platforms], [server] and [healer] are not on the form, and an apply must
    never delete them - so unknown sections are carried through untouched and
    known tables are merged key by key.
    """
    import copy

    merged = copy.deepcopy(existing) if existing else {}
    for key, value in submitted.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


#: A category is matched against a folder name or the first token of a filename,
#: so it can only be a plain word. `[actors.*]` looks like TOML but is neither a
#: valid table name nor something the matcher could ever hit.
TAG_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


def validate_tag_names(document: dict) -> None:
    tags = document.get("tags") or {}
    if not isinstance(tags, dict):
        raise agk.ConfigError("[tags] must be a table of tables")
    for name in tags:
        if TAG_NAME_PATTERN.match(str(name)):
            continue
        cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", str(name)).strip()
        raise agk.ConfigError(
            f"category name {name!r} is not usable. A category is the folder name or "
            f"the filename prefix itself, so type just the word"
            + (f" (did you mean '{cleaned}'?)" if cleaned else "")
            + ". The [tags.<name>] table is written for you; brackets and wildcards "
              "are not part of the name.")


def validate_rules_document(document: dict) -> None:
    """Refuse a document the gate would refuse, before it reaches the disk."""
    agk.Budget.from_mapping(document)              # unknown keys, bad numbers
    validate_tag_names(document)                   # a category must be matchable
    agk.build_tag_budgets(document)                # [tags.*] tables
    named = str(document.get("platform") or "").strip()
    if named:
        # apply_platform raises for an unknown name; that error is the point -
        # `platform = "n64"` without [platforms.n64] refuses to load at all.
        agk.apply_platform(document, named)
    tagging = document.get("tagging") or {}
    mode = tagging.get("mode") if isinstance(tagging, dict) else None
    if mode is not None and mode not in agk.TAG_MODES:
        raise agk.ConfigError(f"[tagging].mode must be one of {', '.join(agk.TAG_MODES)}")
    platforms = document.get("platforms") or {}
    if not isinstance(platforms, dict):
        raise agk.ConfigError("[platforms] must be a table of tables")
    for name in platforms:
        agk.apply_platform(document, name)         # each profile, honouring the whitelist


def write_rules_atomic(rules_path: Path, document: dict) -> str:
    """Write rules.toml atomically and return the text that was written.

    Same directory, ``fsync``, then ``os.replace``: a reader (the gate, CI, an
    editor) sees either the old file or the new one, never half of each. The
    file stays plain text so git keeps a real history of every budget change.
    """
    text = agk.to_toml(document)
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = rules_path.with_name(f".{rules_path.name}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, rules_path)
    return text


class ScanRunner:
    """One background gate run, streamed as Server-Sent Events.

    Explicitly *not* a live slider: a scan costs a full folder parse, so the UI
    asks for it with a button and the server refuses a second concurrent run
    instead of queueing CPU thrash.
    """

    def __init__(self, broadcaster: "Broadcaster") -> None:
        self.broadcaster = broadcaster
        self._lock = threading.Lock()
        self._process = None
        self._thread: threading.Thread | None = None
        self.status = "idle"            # idle | running | done | failed
        self.root = ""
        self.error = ""
        self.started_at = ""
        self.finished_at = ""
        self.totals = {"total": 0, "passed": 0, "failed": 0}
        self._recent: list[dict] = []

    def busy(self) -> bool:
        with self._lock:
            return self.status == "running"

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "root": self.root, "error": self.error,
                    "started_at": self.started_at, "finished_at": self.finished_at,
                    "totals": dict(self.totals)}

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self._recent)

    def _emit(self, event: dict) -> None:
        with self._lock:
            self._recent.append(event)
            del self._recent[:-400]          # a late joiner needs the summary, not history
        self.broadcaster.publish(event)

    def start(self, root: str, rules_path: Path, telemetry_url: str) -> bool:
        with self._lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.root = str(root)
            self.error = ""
            self.started_at = _utc_now()
            self.finished_at = ""
            self.totals = {"total": 0, "passed": 0, "failed": 0}
            self._recent = []
            self._thread = threading.Thread(
                target=self._run, args=(str(root), Path(rules_path), telemetry_url),
                name="agk-scan", daemon=True)
            self._thread.start()
        return True

    def _run(self, root: str, rules_path: Path, telemetry_url: str) -> None:
        command = [sys.executable, "-m", "asset_gatekeeper", root,
                   "--rules", str(rules_path), "--recursive", "--once", "--json",
                   "--no-color", "--no-auto-heal"]
        if telemetry_url:
            command += ["--telemetry-url", telemetry_url]
        environment = dict(os.environ)
        # `-m asset_gatekeeper` must resolve from a checkout as well as a wheel.
        environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
        self._emit({"type": "scan_start", "root": root, "at": self.started_at})
        try:
            process = subprocess.Popen(
                command, cwd=str(REPO_ROOT), env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=(0x08000000 if os.name == "nt" else 0))
        except OSError as exc:
            self._finish("failed", f"cannot start the gate: {exc}")
            return
        with self._lock:
            self._process = process
        assert process.stdout is not None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    self._emit({"type": "log", "text": line[:400]})
                    continue
                if not isinstance(payload, dict):
                    continue
                with self._lock:
                    self.totals["total"] += 1
                    if payload.get("passed"):
                        self.totals["passed"] += 1
                    else:
                        self.totals["failed"] += 1
                    totals = dict(self.totals)
                self._emit({"type": "asset", "name": payload.get("name", "?"),
                            "passed": bool(payload.get("passed")),
                            "metric": payload.get("metric", ""),
                            "problems": list(payload.get("problems") or [])[:4],
                            "totals": totals})
        finally:
            code = process.wait()
            detail = ""
            if process.stderr is not None:
                tail = process.stderr.read() or ""
                detail = tail.strip().splitlines()[-1] if tail.strip() else ""
            self._finish("done" if code == 0 else "failed", detail,
                         exit_code=code)

    def _finish(self, status: str, error: str = "", exit_code: int = 0) -> None:
        with self._lock:
            self.totals["failed"] += 0
            self.status = status
            self.error = error
            self.finished_at = _utc_now()
            self._process = None
            totals = dict(self.totals)
        self._emit({"type": "scan_end", "status": status, "error": error,
                    "exit_code": exit_code, "totals": totals,
                    "at": self.finished_at})


BOOTSTRAP_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Create the first account - AssetGatekeeper</title>
<style>
:root{--bg:#0d1016;--panel:#161b24;--line:#262f3d;--text:#e6e9ef;--muted:#8b94a7;
--accent:#7aa2f7;--bad:#ff6b6b;--good:#3ecf8e}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--text);font:14px/1.55 ui-sans-serif,"Segoe UI",Roboto,Arial,sans-serif}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:26px 26px 22px;width:min(390px,92vw)}
h1{font-size:15px;margin:0 0 4px}
p.sub{color:var(--muted);font-size:12px;margin:0 0 16px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:12px}
input{width:100%;margin-top:5px;background:#1d2431;border:1px solid var(--line);
color:var(--text);border-radius:7px;padding:9px 10px;font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:6px;padding:10px;border-radius:8px;border:1px solid var(--accent);
background:var(--accent);color:#0b1020;font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--bad);font-size:12.5px;margin:0 0 12px}
.hint{color:var(--muted);font-size:11.5px;margin-top:14px;line-height:1.5}
code{background:#0b0f16;border:1px solid var(--line);border-radius:4px;padding:1px 5px}
</style></head><body>
<form method="post" action="/admin/api/bootstrap" autocomplete="on">
  <h1>Create the first account</h1>
  <p class="sub">This dashboard has no accounts yet. The first one is a
  <strong>director</strong>, and it can be created from this machine only.</p>
  <p class="err">{{message}}</p>
  <label>Username<input name="username" value="{{username}}" autofocus required></label>
  <label>Password (at least 10 characters)
    <input name="password" type="password" required minlength="10"></label>
  <label>Confirm password<input name="confirm" type="password" required
    minlength="10"></label>
  <button type="submit">Create account and sign in</button>
  <p class="hint">Only the PBKDF2 hash is stored. Everyone else is added later with
  <code>asset-health --add-admin &lt;name&gt; --admin-role producer</code>.</p>
</body></html>"""


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - AssetGatekeeper admin</title>
<style>
:root{--bg:#0d1016;--panel:#161b24;--line:#262f3d;--text:#e6e9ef;--muted:#8b94a7;
--accent:#7aa2f7;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--text);font:14px/1.55 ui-sans-serif,"Segoe UI",Roboto,Arial,sans-serif}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:26px 26px 22px;width:min(360px,92vw)}
h1{font-size:15px;margin:0 0 4px}
p.sub{color:var(--muted);font-size:12px;margin:0 0 18px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:12px}
input{width:100%;margin-top:5px;background:#1d2431;border:1px solid var(--line);
color:var(--text);border-radius:7px;padding:9px 10px;font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:6px;padding:10px;border-radius:8px;border:1px solid var(--accent);
background:var(--accent);color:#0b1020;font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--bad);font-size:12.5px;margin:0 0 12px}
.hint{color:var(--muted);font-size:11.5px;margin-top:14px}
</style></head><body>
<form method="post" action="/admin/login" autocomplete="on">
  <h1>Rules &amp; Presets</h1>
  <p class="sub">Sign in to change platform budgets.</p>
  <p class="err">{{message}}</p>
  <label>Username<input name="username" value="{{username}}" autofocus required></label>
  <label>Password<input name="password" type="password" required></label>
  <button type="submit">Sign in</button>
  <p class="hint">Accounts are created by whoever administers this dashboard:
  <code>asset-health --add-admin &lt;name&gt; --role director</code></p>
</body></html>"""


def render_login_page(message: str = "", username: str = "",
                      first_run: bool = False) -> str:
    """Sign-in form, or the first-run setup form when no account exists yet.

    A zero-CLI install cannot ask for a terminal to create its first user, so the
    empty state is the setup page. It disappears as soon as one account exists.
    """
    template = BOOTSTRAP_PAGE if first_run else LOGIN_PAGE
    return (template.replace("{{message}}", html.escape(message))
                    .replace("{{username}}", html.escape(username)))


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
                 alert_cooldown: float = DEFAULT_ALERT_COOLDOWN,
                 rules_path: str | Path = "", rules_root: str | Path = "",
                 scan_root: str | Path = "", allow_roots: tuple[str, ...] = (),
                 workstation_url: str = "", workstation_token: str = "",
                 allow_hosts: tuple[str, ...] = (), people: "list[Person] | tuple" = (),
                 digest_at: "tuple[int, int] | None" = None):
        self.db_path = str(db_path)
        self.people = tuple(people)
        self.digest_at = digest_at       # (hour, minute) local, or None for live alerts
        self.auth_token = token          # the studio-wide admin token, optional
        self.quiet = quiet
        # Host header allow-list. The bind address and the loopback names are
        # always accepted; anything else must be named here (--allow-host).
        self.allow_hosts = tuple(str(item).strip().lower() for item in allow_hosts if item)
        # Read-only config the /api/rules inspector reports on. Empty rules_path
        # means "discover rules.toml from rules_root", exactly like the gate does.
        self.rules_path = str(rules_path or "")
        self.rules_root = str(rules_root or Path.cwd())
        self.notifier = notifier
        self.alert_cooldown = float(alert_cooldown)
        self.alerts_suppressed = 0       # visible in /api/summary for tuning
        self.broadcaster = Broadcaster()  # /api/events, one queue per open tab
        self.stopping = False            # flips so stream threads exit on shutdown
        # Admin surface: a second broadcaster so the scan stream is not mixed
        # into the verdict feed, plus per-IP login throttling.
        self.scan_broadcaster = Broadcaster()
        self.scan_root = str(scan_root or Path.cwd())
        # The bridge posts files server-to-server, so the browser never needs
        # CORS or the workstation token.
        self.workstation_url = str(workstation_url or "")
        self.workstation_token = str(workstation_token or "")
        # Where the admin page may point the tool at. A dashboard that can write
        # rules.toml into *any* directory an admin types is an arbitrary-file-write
        # primitive, so the allow-list is the whole guard rail: everything else in
        # this feature is convenience.
        # Normalised and de-duplicated: the launcher seeds this from --allow-root
        # *and* $AGK_PROJECT_ROOTS, so the same folder arriving twice is normal,
        # and two identical entries in the picker look like a bug (they were).
        roots: list[Path] = []
        seen: set[str] = set()
        for item in allow_roots:
            text = str(item).strip().strip('"')
            if not text:
                continue
            resolved = Path(text).expanduser().resolve()
            key = str(resolved).lower() if os.name == "nt" else str(resolved)
            if key in seen:
                continue
            seen.add(key)
            roots.append(resolved)
        if not roots:
            roots = [Path.cwd().resolve()]
        self.allow_roots: tuple[Path, ...] = tuple(roots)
        self.scanner = ScanRunner(self.scan_broadcaster)
        self._login_lock = threading.Lock()
        self._login_failures: dict[str, list[float]] = {}
        super().__init__(address, Handler)

    def shutdown(self) -> None:          # noqa: D401 - stdlib override
        self.stopping = True
        super().shutdown()


def build_server(host: str, port: int, db_path: str | Path,
                 token: str = "", quiet: bool = False,
                 notifier: "Notifier | None" = None,
                 alert_cooldown: float = DEFAULT_ALERT_COOLDOWN,
                 rules_path: str | Path = "", rules_root: str | Path = "",
                 scan_root: str | Path = "",
                 allow_roots: tuple[str, ...] = (),
                 workstation_url: str = "",
                 workstation_token: str = "",
                 allow_hosts: tuple[str, ...] = (),
                 audit=None, people: "list[Person] | tuple" = (),
                 digest_at: "tuple[int, int] | None" = None) -> AssetHealthServer:
    if audit is not None:
        set_audit_log(audit)
    server = AssetHealthServer((host, port), db_path, token, quiet, notifier, alert_cooldown,
                               rules_path, rules_root, scan_root, allow_roots,
                               workstation_url, workstation_token, allow_hosts,
                               people=people, digest_at=digest_at)
    if digest_at and notifier is not None:
        threading.Thread(target=_digest_loop, args=(server,), name="ah-digest",
                         daemon=True).start()
    return server


def describe_rules(server: "AssetHealthServer") -> dict:
    """The read-only rules snapshot, or an error object instead of a 500.

    A broken rules.toml must not blank the inspector: the dashboard is where an
    artist goes to find out *why* their asset failed, so a config error is
    reported as data, with the message the gate would print.
    """
    try:
        return agk.describe_rules(Path(server.rules_root),
                                  server.rules_path or None)
    except (agk.ConfigError, SystemExit) as exc:
        return {"read_only": True,
                "error": f"cannot read rules: {exc}",
                "source": server.rules_path or "built-in defaults",
                "global_budget": None,
                "tag_budgets": []}


# --- #
# Seed data - so the dashboard is not an empty page on first run
# --- #

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
        # Counted so the dashboard can label them. Demo rows look exactly like
        # real ones, which is how a producer ends up arguing about a pass rate
        # that was never theirs.
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('demo_rows', ?)",
                           (str(len(_SEED) + 1),))
    return True


def counts_zero(db_path: str | Path) -> bool:
    """True when nothing has been ingested yet, so the CLI can suggest a command."""
    try:
        with open_db(db_path) as connection:
            return connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
    except STORAGE_ERRORS:
        return False


def demo_row_count(db_path: str | Path) -> int:
    """How many verdicts in this database came from the bundled demo set."""
    try:
        with open_db(db_path) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'demo_rows'").fetchone()
    except STORAGE_ERRORS:
        return 0
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


# --- #
# CLI
# --- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset_health_server",
        description="Studio dashboard for AssetGatekeeper verdicts (stdlib only).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to bind (default {DEFAULT_HOST}; 0.0.0.0 exposes it)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--port-auto", action="store_true",
                        help="if the port is busy, move to the next free one instead of "
                             "failing (prints what it chose; keep telemetry URLs in step)")
    parser.add_argument("--port-tries", type=int, default=10, metavar="N",
                        help="how many ports --port-auto may walk past (default 10)")
    parser.add_argument("--db",
                        default=(os.environ.get("ASSET_HEALTH_DB")
                                 or os.environ.get("AGK_DB_URL") or DEFAULT_DB),
                        help=f"SQLite file or postgresql:// DSN "
                             f"(default {DEFAULT_DB}, or $ASSET_HEALTH_DB / $AGK_DB_URL)")
    parser.add_argument("--enterprise-config", metavar="PATH", default="",
                        help="enterprise.toml: audit forwarding, read replicas, "
                             "SSO (default: ./enterprise.toml if present)")
    parser.add_argument("--token", default=os.environ.get(DEFAULT_TOKEN_ENV, ""),
                        help=f"require 'Authorization: Bearer <token>' (default ${DEFAULT_TOKEN_ENV})")
    parser.add_argument("--workstation-url", metavar="URL",
                        default=os.environ.get("AGK_WORKSTATION_URL", ""),
                        help="Art Workstation base URL used by 'Send to Workstation' "
                             "(default: the running one recorded in .agk/ports.json, "
                             "or $AGK_WORKSTATION_URL)")
    parser.add_argument("--workstation-token", metavar="TOKEN",
                        default=os.environ.get("AGK_WORKSTATION_TOKEN", ""),
                        help="token for that workstation, when it runs with AUTH_TOKEN")
    parser.add_argument("--demo", action="store_true",
                        help="fill a brand-new database with the bundled demo verdicts "
                             "(716 passed / 64 failed) so the dashboard is not empty")
    parser.add_argument("--no-seed", action="store_true",
                        help="accepted for old scripts; demo data is off unless --demo is given")
    parser.add_argument("--quiet", action="store_true", help="only log errors")
    parser.add_argument("--allow-root", metavar="PATH", action="append", default=[],
                        help="folder the admin page may open as a project (repeatable; "
                             "default: the working directory). Also settable with "
                             f"${ALLOW_ROOTS_ENV}, separated by '{os.pathsep}'")
    parser.add_argument("--allow-host", metavar="NAME", action="append", default=[],
                        help="accept this Host header as well as the bind address and "
                             "localhost (repeatable). Requests whose Host names something "
                             "else are refused: that is what stops a DNS-rebinding page "
                             "from driving the dashboard")
    parser.add_argument("--scan-root", metavar="PATH", default="",
                        help="folder 'Apply & Rescan' scans (default: the rules root)"
                             "; fixed at startup so the admin page can never point "
                             "the server at an arbitrary directory")
    admin = parser.add_argument_group("admin accounts")
    admin.add_argument("--add-admin", metavar="USER", default="",
                       help="create a rules-admin account; prints its password once")
    admin.add_argument("--admin-role", default="director",
                       choices=("director", "producer", "artist"),
                       help="role for --add-admin (default director)")
    admin.add_argument("--list-admins", action="store_true",
                       help="list admin accounts and exit")
    parser.add_argument("--rules", metavar="PATH", default=os.environ.get(RULES_ENV, ""),
                        help="rules.toml that GET /api/rules reports on, read-only "
                             f"(default: ./{agk.RULES_FILENAME} in the working directory, "
                             f"or ${RULES_ENV})")

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
    alerts.add_argument("--people", metavar="PATH", default=os.environ.get(PEOPLE_ENV, ""),
                        help="people.toml mapping user and machine names to chat handles, so "
                             f"an alert mentions the artist (or ${PEOPLE_ENV})")
    alerts.add_argument("--alert-digest", metavar="HH:MM", default="",
                        help="instead of one message per failure, one digest a day at this "
                             "local time, a message per person")
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
    # A DSN selects PostgreSQL; anything else is a file path, exactly as before.
    if is_dsn(args.db):
        db_path: str | Path = args.db
    else:
        db_path = Path(args.db).expanduser()
        if db_path.parent and not db_path.parent.exists():
            print(f"error: database folder does not exist: {db_path.parent}",
                  file=sys.stderr)
            return 2
    try:
        init_db(db_path)
    except STORAGE_ERRORS as exc:
        print(f"error: cannot initialise {db_path}: {exc}", file=sys.stderr)
        return 2

    # Enterprise configuration is optional. A broken file is not: refuse to
    # start rather than run with a half-applied policy.
    try:
        enterprise = load_config(getattr(args, "enterprise_config", "") or None)
    except EnterpriseConfigError as exc:
        print(f"error: enterprise config: {exc}", file=sys.stderr)
        return 2
    if enterprise.warnings:
        for warning in enterprise.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    audit = build_audit_log(enterprise.audit)
    set_audit_log(audit)
    if enterprise.audit.enabled:
        print(f"  audit      {', '.join(audit.stats()['emitters']) or 'disabled'}")
    try:
        store_for(db_path).start_health()
    except STORAGE_ERRORS as exc:      # a replica that cannot connect is not fatal
        print(f"warning: health loop not started: {exc}", file=sys.stderr)

    # Management commands run and exit; they never bind a port.
    if args.list_admins:
        rows = list_admin_users(db_path)
        if not rows:
            print("no admin accounts yet. create one with:")
            print("  asset-health --add-admin <name> --role director")
        for row in rows:
            flag = "  (disabled)" if row["disabled"] else ""
            print(f"  {row['username']:<22}{row['role']:<11}created {row['created_at']}{flag}")
        return 0

    if args.add_admin:
        try:
            password = create_admin_user(db_path, args.add_admin, args.admin_role)
        except (ValueError, sqlite3.IntegrityError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"admin '{args.add_admin}' created with role '{args.admin_role}'")
        print(f"  password (shown once): {password}")
        print(f"  sign in at  http://{args.host}:{args.port}/admin/login")
        print("  only the PBKDF2 hash is stored; this password cannot be recovered")
        return 0

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

    # Demo data is opt-in. It used to be the default, and the first thing every
    # new user asked was why the dashboard already had 780 verdicts they never
    # ran: a pass rate that is not yours is worse than an empty page.
    seeded = False
    if args.demo and not args.no_seed:
        try:
            seeded = seed_if_first_launch(db_path)
        except STORAGE_ERRORS as exc:
            print(f"warning: seeding skipped ({exc})", file=sys.stderr)

    webhook_url, webhook_provider, alert_cooldown = resolve_webhook(args)
    notifier = Notifier(webhook_url, webhook_provider) if webhook_url else None
    people: list = []
    if args.people:
        try:
            people = load_people(args.people)
        except (OSError, ValueError, agk.ConfigError) as exc:
            print(f"error: cannot read people: {exc}", file=sys.stderr)
            return 2
    digest_at = None
    if args.alert_digest:
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", args.alert_digest.strip())
        if not match:
            print("error: --alert-digest takes a time like 09:00", file=sys.stderr)
            return 2
        digest_at = (int(match.group(1)), int(match.group(2)))
        if notifier is None:
            print("warning: --alert-digest has no webhook to post to", file=sys.stderr)

    def _moved(old: int, new: int, exc: BaseException) -> None:
        print(f"note: port {old} is busy ({exc.__class__.__name__}), using {new}",
              file=sys.stderr)

    try:
        httpd, bound_port = agk.bind_with_fallback(
            lambda host, port: build_server(host, port, db_path, args.token, args.quiet,
                                            notifier, alert_cooldown,
                                            rules_path=args.rules, rules_root=Path.cwd(),
                                            scan_root=args.scan_root or Path.cwd(),
                                            allow_roots=tuple(args.allow_root) + tuple(
                                                item for item in os.environ.get(
                                                    ALLOW_ROOTS_ENV, "").split(os.pathsep)
                                                if item.strip()),
                                            allow_hosts=tuple(args.allow_host),
                                            # Both flags were parsed and then never
                                            # passed on, so Send to Workstation only
                                            # worked via agk-serve's .agk/ports.json.
                                            workstation_url=args.workstation_url,
                                            workstation_token=args.workstation_token,
                                            people=people, digest_at=digest_at),
            args.host, args.port, auto=args.port_auto, tries=args.port_tries,
            reserved=(ART_WORKSTATION_PORT,), report=_moved)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        print(agk.port_in_use_hint(args.port), file=sys.stderr)
        if not args.port_auto:
            print("      or let it move by itself: --port-auto", file=sys.stderr)
        return 2

    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    projects = list_projects(db_path)
    print(f"AssetHealth listening on http://{shown}:{port}/dashboard")
    print(f"  database   {db_path if isinstance(db_path, str) else db_path.resolve()}")
    print("  ingest     POST /api/telemetry")
    print(f"  rules      GET /api/rules  ({(Path(args.rules).resolve() if args.rules else Path.cwd() / agk.RULES_FILENAME)})")
    print(f"  telemetry  asset-gatekeeper <folder> --once --telemetry-url "
          f"http://{shown}:{port}/api/telemetry")
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
        print(f"  seeded     {len(_SEED) + 1:,} demo verdicts (--demo) - they are labelled")
        print("             on the dashboard; use --db project.db for a clean page")
    elif not projects and counts_zero(db_path):
        print("  empty      no verdicts yet - point the gate at it:")
        print(f"             asset-gatekeeper <folder> --once --telemetry-url "
              f"http://{shown}:{port}/api/telemetry")

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
