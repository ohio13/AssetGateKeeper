"""Storage refactor + analytics page: SQLite and PostgreSQL must agree.

The PostgreSQL half runs against a driver *double*: a psycopg3-shaped module that
executes the translated SQL on SQLite. That is enough to prove three things that
actually break in production - the server's SQL survives the translator, the rows
come back in a shape the dashboard can read, and both backends return identical
analytics for identical data. It is deliberately not a claim about PostgreSQL
semantics; a real server is the only way to test those, and the suite says so.

    python tests/test_analytics.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "server"), str(ROOT / "engine")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

WORK = Path("scratch") / "analytics_tests"
if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


# --------------------------------------------------------------------------- #
# A psycopg3-shaped driver that runs the translated SQL on SQLite
# --------------------------------------------------------------------------- #
DDL_SEEN: list[str] = []
DETAIL = {"wrong": []}


_PG_TIME_FORMATS = {
    'YYYY-MM-DD"T"HH24': "%Y-%m-%dT%H",
    "YYYY-MM-DD": "%Y-%m-%d",
    'IYYY-"W"IW': "%Y-W%W",
    "YYYY-MM": "%Y-%m",
    "YYYY": "%Y",
}


def _unpostgres(sql: str) -> str:
    """Translate the PostgreSQL-only functions this project emits back to SQLite.

    Without this the double would fail on `to_char(...::timestamptz, ...)`, which
    says nothing about our code and everything about the double.
    """
    import re as _re

    def to_char(match):
        column, fmt = match.group(1), match.group(2)
        return f"strftime('{_PG_TIME_FORMATS.get(fmt, '%Y-%m-%d')}', {column})"

    sql = _re.sub(r"to_char\((\w+)::timestamptz, '([^']*)'\)", to_char, sql)
    sql = _re.sub(r"string_agg\(([^,]+), chr\(10\)\)", r"group_concat(\1, char(10))", sql)
    return sql


class FakeCursor:
    def __init__(self, raw):
        self._raw = raw
        self._cursor = None
        self.description = None
        self.rowcount = -1

    def _fail(self, reason):
        DETAIL["wrong"].append(reason)
        raise AssertionError(reason)

    def execute(self, sql, params=()):
        text = str(sql)
        upper = text.lstrip().upper()
        if "?" in text and params:
            self._fail(f"untranslated placeholder reached the driver: {text[:80]}")
        if "INSERT OR REPLACE" in upper:
            self._fail(f"SQLite upsert syntax reached PostgreSQL: {text[:80]}")

        # Schema and session settings: recorded, not executed. The test creates the
        # tables itself, so what matters is the shape of the DDL we send.
        if upper.startswith(("CREATE ", "ALTER ", "DROP ", "SET ", "PRAGMA")):
            DDL_SEEN.append(text)
            self.description = None
            self.rowcount = 0
            return self

        if "TO_CHAR(" in text.upper() or "STRING_AGG(" in text.upper():
            text = _unpostgres(text)

        if "RETURNING" in text.upper():
            # SQLite before 3.35 has no RETURNING; emulate the one shape we send.
            stripped = text[:text.upper().index("RETURNING")].rstrip().rstrip(";")
            cursor = self._raw.execute(stripped.replace("%s", "?"), tuple(params or ()))
            self._raw.commit()
            self._rows = [{"id": cursor.lastrowid}]
            self.description = [("id",)]
            self.rowcount = 1
            return self

        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            table = str(params[0]) if params else ""
            rows = self._raw.execute(f"PRAGMA table_info({table})").fetchall()
            self._rows = [{"column_name": row[1]} for row in rows]
            self.description = [("column_name",)]
            self.rowcount = len(self._rows)
            return self

        self._cursor = self._raw.execute(text.replace("%s", "?"), tuple(params or ()))
        self.description = self._cursor.description
        self.rowcount = self._cursor.rowcount
        self._rows = ([] if self.description is None
                      else [dict(zip([col[0] for col in self.description], row))
                            for row in self._cursor.fetchall()])
        return self

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)
        return self.rowcount

    def fetchall(self):
        return list(getattr(self, "_rows", []))

    def fetchone(self):
        rows = getattr(self, "_rows", [])
        return rows[0] if rows else None

    def close(self):
        self._cursor = None


class FakeConnection:
    def __init__(self, path):
        self._raw = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self.closed = False
        self.broken = False

    def cursor(self):
        return FakeCursor(self._raw)

    def execute(self, sql, params=()):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self.closed = True
        self._raw.close()


DSNS: dict[str, Path] = {}


def _connect(dsn, row_factory=None):
    path = DSNS.get(str(dsn))
    if path is None:
        raise AssertionError(f"unknown fake DSN {dsn}")
    return FakeConnection(str(path))


def install_fake_psycopg():
    module = types.ModuleType("psycopg")
    rows_module = types.ModuleType("psycopg.rows")
    rows_module.dict_row = dict
    module.rows = rows_module
    module.connect = _connect
    sys.modules["psycopg"] = module
    sys.modules["psycopg.rows"] = rows_module
    sys.modules["psycopg_pool"] = None          # no pool package: use our own


install_fake_psycopg()

import asset_health_server as ahs           # noqa: E402
import enterprise.store as store            # noqa: E402

print("1. the storage seam")


def new_db(name: str) -> Path:
    path = WORK / f"{name}.db"
    ahs.init_db(path)
    return path


sqlite_db = new_db("seam")
with ahs.open_db(sqlite_db) as connection:
    expect("open_db yields a sqlite3 connection for a file path",
           isinstance(connection, sqlite3.Connection))
    expect("the legacy row shape is still available",
           connection.execute("SELECT 1 AS one").fetchone()["one"] == 1)

first = ahs.store_for(sqlite_db)
second = ahs.store_for(str(sqlite_db))
expect("store_for memoises the Store", first is second)
expect("a SQLite Store reports its engine", first.is_sqlite is True)

expect("is_dsn recognises a PostgreSQL URL", store.is_dsn("postgresql://u@h/db"))
expect("is_dsn rejects a file path", not store.is_dsn("metrics.db"))
expect("postgres:// is accepted too", store.is_dsn("postgres://u@h/db"))

columns = None
with ahs.open_db(sqlite_db) as connection:
    columns = ahs.table_columns(connection, "verdicts")
expect("table_columns reads the SQLite schema", {"id", "timestamp", "status"} <= columns,
       str(sorted(columns)[:5]))

with ahs.open_db(sqlite_db) as connection:
    new_id = ahs.insert_id(connection, "INSERT INTO meta (key, value) VALUES (?, ?)",
                           ("probe", "1"))
expect("insert_id returns the new row id on SQLite", new_id > 0, str(new_id))

import os  # noqa: E402
os.environ["AGK_DB_URL"] = "postgresql://env/db"
env_default = ahs.build_parser().parse_args([]).db
del os.environ["AGK_DB_URL"]
expect("--db defaults from AGK_DB_URL", env_default == "postgresql://env/db", env_default)
expect("--db still accepts a file path",
       ahs.build_parser().parse_args(["--db", "x.db"]).db == "x.db")
expect("the parser documents both backends", "postgresql://" in
       [a for a in ahs.build_parser()._actions if a.dest == "db"][0].help)

# --------------------------------------------------------------------------- #
# Identical data on both backends
# --------------------------------------------------------------------------- #
print("2. both backends, identical data")

PG_DSN = "postgresql://fake/parity"
PG_PATH = WORK / "parity.db"
DSNS[PG_DSN] = PG_PATH
sqlite3.connect(str(PG_PATH)).close()
# The fake driver ignores DDL, so the PostgreSQL target is created with the same
# schema the server ships. This is the one thing the double cannot prove.
with sqlite3.connect(str(PG_PATH)) as raw:
    raw.executescript(ahs.SCHEMA)
# Then let the server submit its own schema, so the DDL it sends is recorded and
# can be asserted (the double records rather than executes CREATE/ALTER).
ahs.init_db(PG_DSN)

now = datetime.now(timezone.utc)
ROWS = [
    ("env/rock.obj", False, "72,500 tris / 4 mat", 80,
     ["72,500 triangles exceeds the 50,000 budget (+22,500)",
      "4 material slots exceeds the 3 slot budget (draw calls)"]),
    ("env/crag.obj", True, "18,000 tris / 2 mat", 75, []),
    ("char/hero.obj", True, "30,000 tris / 3 mat", 45, []),
    ("char/boss.obj", False, "120,400 tris / 6 mat", 40,
     ["120,400 triangles exceeds the 50,000 budget (+70,400)"]),
    ("props/crate.obj", True, "900 tris / 1 mat", 20, []),
    ("env/rock.obj", False, "72,500 tris / 4 mat", 10,
     ["72,500 triangles exceeds the 50,000 budget (+22,500)"]),
]


def seed(target):
    for filename, passed, metric, days_ago, problems in ROWS:
        stamp = (now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        ahs.store_verdict(target, {"timestamp": stamp, "name": filename,
                                   "passed": passed, "metric": metric,
                                   "status": "PASSED" if passed else "FAILED",
                                   "problems": problems, "username": "ana",
                                   "hostname": "ws1"})


seed(sqlite_db)
seed(PG_DSN)

expect("the sqlite store counted every row",
       ahs.totals(sqlite_db)["total"] == len(ROWS), str(ahs.totals(sqlite_db)))
with ahs.open_db(PG_DSN) as connection:
    expect("the PostgreSQL path returns rows the dashboard can index by name or position",
           connection.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"] == len(ROWS))
    row = connection.execute("SELECT filename FROM verdicts ORDER BY id LIMIT 1").fetchone()
    expect("positional access still works on PostgreSQL rows", row[0] == "env/rock.obj", str(row[0]))

sqlite_report = ahs.analytics_report(sqlite_db, None, "90d", "month")
pg_report = ahs.analytics_report(PG_DSN, None, "90d", "month")
expect("compliance is identical on both backends",
       sqlite_report["compliance"] == pg_report["compliance"],
       f"{sqlite_report['compliance']} vs {pg_report['compliance']}")
expect("the summary is identical", sqlite_report["summary"] == pg_report["summary"])
expect("polygon growth is identical", sqlite_report["growth"] == pg_report["growth"])
expect("error clusters are identical", sqlite_report["errors"] == pg_report["errors"])
expect("the report has the three sections",
       {"compliance", "growth", "errors", "summary"} <= set(sqlite_report))
expect("compliance has the monthly buckets", len(sqlite_report["compliance"]["points"]) == 3,
       str([p["bucket"] for p in sqlite_report["compliance"]["points"]]))
expect("the failing month is reported as such",
       any(point["failed"] > 0 for point in sqlite_report["compliance"]["points"]))
expect("growth carries average and peak",
       all({"average_triangles", "peak_triangles"} <= set(point)
           for point in sqlite_report["growth"]["points"]))
expect("different assets with the same message collapse into one cluster",
       any(cluster["count"] == 3 for cluster in sqlite_report["errors"]),
       str([c["count"] for c in sqlite_report["errors"]]))
expect("clusters carry their signature window",
       all(len(cluster["first_seen"]) >= 10 for cluster in sqlite_report["errors"]))

# --------------------------------------------------------------------------- #
# 3. The HTTP surface
# --------------------------------------------------------------------------- #
print("3. /analytics and /api/analytics")


def request(port, path, method="GET", body=None, timeout=10):
    data = None if body is None else (body if isinstance(body, bytes)
                                      else json.dumps(body).encode())
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def start(db_target):
    server = ahs.build_server("127.0.0.1", 0, db_target, quiet=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    return server, server.server_address[1]


server, port = start(sqlite_db)
try:
    status, body, headers = request(port, "/api/analytics?since=90d&bucket=month")
    payload = json.loads(body)
    expect("GET /api/analytics is JSON", status == 200 and headers.get("Content-Type", "").startswith("application/json"),
           str(status))
    expect("the endpoint reports its window",
           payload["window"]["since"] == "90d" and payload["window"]["bucket"] == "month")
    expect("the JSON matches the function",
           payload["compliance"]["points"] == sqlite_report["compliance"]["points"])

    status, body, _ = request(port, "/analytics?since=90d&bucket=month")
    page = body.decode("utf-8")
    expect("GET /analytics renders HTML", status == 200 and page.startswith("<!DOCTYPE html>"),
           str(status))
    expect("the page shows the compliance section", "Compliance" in page)
    expect("the page shows polygon growth", "Polygon growth" in page)
    expect("the page shows error clusters", "Error clusters" in page)
    expect("the page prints a bucket row", "2024" in page or page.count("<tr>") > 3)
    expect("the page counts the verdicts", f">{len(ROWS)}<" in page or f"{len(ROWS):,}" in page)
    expect("the page links the JSON", "/api/analytics" in page)
    expect("the page links back to the dashboard", "/dashboard" in page)
    expect("the failing asset's message reaches the page",
           "exceeds the 50,000 budget" in page)
    expect("the page marks the active window", 'class="on"' in page)

    status, body, _ = request(port, "/analytics?since=7d")
    expect("a short window is accepted", status == 200, str(status))
    status, body, _ = request(port, "/analytics?since=banana&bucket=nope")
    expect("junk parameters fall back instead of erroring", status == 200, str(status))

    status, body, _ = request(port, "/api/analytics?project_id=999")
    expect("an unknown project is a 404", status == 404, str(status))

    status, body, _ = request(port, "/analytics")
    expect("the dashboard links analytics", b"/analytics" in
           request(port, "/dashboard")[1], str(status))
finally:
    server.shutdown()
    server.server_close()
    ahs.close_stores()

# --------------------------------------------------------------------------- #
# 4. The same server on the PostgreSQL backend
# --------------------------------------------------------------------------- #
print("4. the server on PostgreSQL")

ahs.close_stores()
pg_server, pg_port = start(PG_DSN)
try:
    status, body, _ = request(pg_port, "/api/analytics?since=90d&bucket=month")
    pg_payload = json.loads(body)
    expect("the PostgreSQL-backed server answers /api/analytics", status == 200, str(status))
    # "90d" is resolved against the wall clock on each call, so the windows can
    # differ by a second; the numbers they contain must not.
    expect("its compliance equals the SQLite server's",
           pg_payload["compliance"]["points"] == sqlite_report["compliance"]["points"],
           f"{pg_payload['compliance']['points'][:1]}")
    expect("both windows are the same relative day",
           pg_payload["window"]["since"][:13] == sqlite_report["window"]["since"][:13])
    expect("its growth equals the SQLite server's",
           pg_payload["growth"]["points"] == sqlite_report["growth"]["points"],
           f"pg={str(pg_payload['growth']['points'])[:110]}")
    expect("its summary equals the SQLite server's",
           pg_payload["summary"] == sqlite_report["summary"])
    expect("its clusters equal the SQLite server's",
           pg_payload["errors"] == sqlite_report["errors"])

    status, body, _ = request(pg_port, "/dashboard")
    expect("the dashboard renders on PostgreSQL", status == 200 and b"AssetHealth" in body,
           str(status))
    status, body, _ = request(pg_port, "/api/summary")
    expect("the summary endpoint works on PostgreSQL", status == 200, str(status))
    summary_pg = json.loads(body)
    expect("the summary endpoint answers with its documented shape",
           isinstance(summary_pg, dict) and {"totals", "alerts"} <= set(summary_pg),
           str(sorted(summary_pg))[:120])
    expect("the summary counted every seeded verdict",
           int(summary_pg.get("totals", {}).get("total", -1)) == len(ROWS),
           str(summary_pg.get("totals")))

    status, body, _ = request(pg_port, "/api/telemetry", method="POST",
                              body={"name": "env/new.obj", "passed": True,
                                    "metric": "1,200 tris / 1 mat",
                                    "timestamp": datetime.now(timezone.utc).isoformat(
                                        timespec="seconds")})
    expect("ingest works through the PostgreSQL path", status == 201, str(status))
    expect("the new row is visible immediately",
           json.loads(request(pg_port, "/api/analytics?since=7d&bucket=day")[1])
           ["compliance"]["points"][-1]["total"] >= 1)
finally:
    pg_server.shutdown()
    pg_server.server_close()
    ahs.close_stores()

print("5. what the PostgreSQL driver was actually sent")
expect("the driver never saw an untranslated placeholder", not DETAIL["wrong"],
       "; ".join(DETAIL["wrong"][:2]))
creates = [statement for statement in DDL_SEEN if statement.upper().startswith("CREATE TABLE")]
alters = [statement for statement in DDL_SEEN if "ADD CONSTRAINT" in statement.upper()]
expect("the schema was sent as PostgreSQL DDL", any("IDENTITY" in s.upper() for s in creates))
expect("no AUTOINCREMENT survived the translation",
       all("AUTOINCREMENT" not in s.upper() for s in creates))
expect("foreign keys were moved to a second pass", alters, str(len(alters)))
expect("inline REFERENCES are absent from the CREATE statements",
       all("REFERENCES" not in s for s in creates), str(len(creates)))

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
