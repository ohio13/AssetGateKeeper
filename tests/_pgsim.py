"""A psycopg3-shaped driver double that executes translated SQL on SQLite.

Shared by the enterprise and analytics suites. It proves the SQL our code *emits*
survives translation and that the rows we read have the right shape; it says
nothing about PostgreSQL's own semantics, and every test that uses it says so.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import types
from pathlib import Path

_PG_FORMATS = {'YYYY-MM-DD"T"HH24': "%Y-%m-%dT%H", "YYYY-MM-DD": "%Y-%m-%d",
               'IYYY-"W"IW': "%Y-W%W", "YYYY-MM": "%Y-%m", "YYYY": "%Y"}


def _unpostgres(sql: str) -> str:
    def to_char(match):
        return f"strftime('{_PG_FORMATS.get(match.group(2), '%Y-%m-%d')}', {match.group(1)})"

    sql = re.sub(r"to_char\((\w+)::timestamptz, '([^']*)'\)", to_char, sql)
    return re.sub(r"string_agg\(([^,]+), chr\(10\)\)", r"group_concat(\1, char(10))", sql)


class _Registry:
    """Where the double records what it was asked to do."""

    def __init__(self) -> None:
        self.ddls: list[str] = []
        self.violations: list[str] = []
        self.connect_failures: dict[str, int] = {}
        #: DSNs whose server has "gone away": new *and* pooled connections fail,
        #: which is what a failover actually looks like.
        self.dead_dsns: set[str] = set()
        self.connections = 0
        self.lock = None


REGISTRY = _Registry()


class Cursor:
    def __init__(self, raw):
        self._raw = raw
        self._cursor = None
        self._rows: list[dict] = []
        self.description = None
        self.rowcount = -1

    def execute(self, sql, params=()):
        text = str(sql)
        upper = text.lstrip().upper()
        if "?" in text and params:
            REGISTRY.violations.append(f"untranslated placeholder: {text[:70]}")
        if "INSERT OR REPLACE" in upper:
            REGISTRY.violations.append(f"sqlite upsert syntax: {text[:70]}")
        if upper.startswith(("CREATE ", "ALTER ", "DROP ", "SET ", "PRAGMA")):
            REGISTRY.ddls.append(text)
            self.description = None
            self.rowcount = 0
            return self
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            rows = self._raw.execute(f"PRAGMA table_info({params[0]})").fetchall()
            self._rows = [{"column_name": row[1]} for row in rows]
            self.description = [("column_name",)]
            self.rowcount = len(self._rows)
            return self
        if "TO_CHAR(" in upper or "STRING_AGG(" in upper:
            text = _unpostgres(text)
        if "RETURNING" in text.upper():
            stripped = text[:text.upper().index("RETURNING")].rstrip().rstrip(";")
            cursor = self._raw.execute(stripped.replace("%s", "?"), tuple(params or ()))
            self._raw.commit()
            self._rows = [{"id": cursor.lastrowid}]
            self.description = [("id",)]
            self.rowcount = 1
            return self
        self._cursor = self._raw.execute(text.replace("%s", "?"), tuple(params or ()))
        self.description = self._cursor.description
        self.rowcount = self._cursor.rowcount
        self._rows = ([] if self.description is None else
                      [dict(zip([c[0] for c in self.description], row))
                       for row in self._cursor.fetchall()])
        return self

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)
        return self.rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self._cursor = None


class Connection:
    def __init__(self, path: Path, dsn: str = "") -> None:
        self._raw = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0)
        self.closed = False
        self.broken = False
        self._dsn = dsn

    def _check(self) -> None:
        if self._dsn in REGISTRY.dead_dsns:
            raise sqlite3.OperationalError("server closed the connection unexpectedly")

    def cursor(self):
        self._check()
        return Cursor(self._raw)

    def execute(self, sql, params=()):
        self._check()
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
    dsn = str(dsn)
    failures = REGISTRY.connect_failures.get(dsn, 0)
    if failures:
        REGISTRY.connect_failures[dsn] = failures - 1
        raise sqlite3.OperationalError(f"connection refused: {dsn}")
    if dsn in REGISTRY.dead_dsns:
        raise sqlite3.OperationalError(f"could not connect to {dsn}")
    path = DSNS.get(dsn)
    if path is None:
        raise AssertionError(f"unknown fake DSN {dsn}")
    REGISTRY.connections += 1
    return Connection(path, dsn)


def install(mapping: dict[str, Path]) -> None:
    """Register DSN -> SQLite file and insert the fake psycopg modules."""
    DSNS.update(mapping)
    module = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = dict
    module.rows = rows
    module.connect = _connect
    sys.modules["psycopg"] = module
    sys.modules["psycopg.rows"] = rows
    sys.modules["psycopg_pool"] = None


def create(path: Path, schema_sql: str = "") -> Path:
    """Create the SQLite file behind a fake DSN, optionally with a schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as raw:
        if schema_sql.strip():
            raw.executescript(schema_sql)
    return path
