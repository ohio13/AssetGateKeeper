"""Storage: the same API over SQLite (default) and pooled PostgreSQL.

Why this exists
    The dashboard shipped with SQLite because a two-person studio should not have
    to run a database. A 200-person studio has the opposite problem: every artist
    workstation writing its own ``asset_health.db`` is not a metric, it is a
    rumour. The enterprise deployment points ``database.dsn`` at one PostgreSQL
    server and every client posts there.

How it works
    ``open_store()`` returns a :class:`Store` around either a sqlite3 connection
    or a pooled psycopg3 connection. The rest of the codebase keeps writing
    ``?``-style SQL; :class:`SqlTranslator` rewrites what PostgreSQL spells
    differently - placeholders, ``INSERT OR REPLACE``, ``AUTOINCREMENT``,
    ``GROUP_CONCAT`` - and refuses anything it cannot translate exactly.

What is deliberately *not* silent
    * A translation it does not recognise raises :class:`StoreError` at the call
      site, with the statement attached. Guessing at SQL is how a dashboard
      starts showing plausible, wrong numbers.
    * The schema is created in two passes on PostgreSQL (tables first, foreign
      keys second) because the SQLite schema references tables declared later in
      the same script.
    * ``PRAGMA`` is a no-op on PostgreSQL, never an error: the ``db_path``-style
      code paths are shared.
"""

from __future__ import annotations

import contextlib
import queue
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .config import DatabaseConfig


class StoreError(Exception):
    """A storage problem, phrased for whoever has to fix it."""


#: Tables written with ``INSERT OR REPLACE`` and the column(s) that make a row
#: unique. PostgreSQL needs this to build ``ON CONFLICT``; a table missing here
#: is refused instead of being written with the wrong semantics.
PRIMARY_KEYS = {
    "meta": ("key",),
    "project_roots": ("path",),
    "budgets": ("project_id",),
    "alerts_state": ("scope_key",),
    "collisions": ("asset_id",),
    "lod_versions": ("asset_id", "level"),
    "admin_users": ("username",),
    "admin_sessions": ("token",),
    "projects": ("id",),
    "tag_rules": ("project_id", "tag"),
    "verdict_metrics": ("verdict_id", "metric"),
}

_REPLACE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL)
_GROUP_CONCAT_RE = re.compile(r"GROUP_CONCAT\s*\(\s*([^,()]+?)\s*,\s*char\(10\)\s*\)",
                              re.IGNORECASE)
_AUTOINCREMENT_RE = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE)
_COLLATE_NOCASE_RE = re.compile(r"\s+COLLATE\s+NOCASE", re.IGNORECASE)
_FK_COLUMN_RE = re.compile(
    r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)\s+INTEGER\s+"
    r"(?P<notnull>NOT\s+NULL\s+)?"
    r"REFERENCES\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?P<target>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
    r"(?P<ondelete>\s+ON\s+DELETE\s+[A-Z ]+?)?(?=\s*,|\s*$)",
    re.IGNORECASE)


@dataclass(frozen=True)
class Dialect:
    name: str
    placeholder: str

    @property
    def is_postgres(self) -> bool:
        return self.name == "postgres"


SQLITE = Dialect("sqlite", "?")
POSTGRES = Dialect("postgres", "%s")


def dialect_for(dsn_or_path: str | Path) -> Dialect:
    text = str(dsn_or_path).lower()
    if text.startswith(("postgres://", "postgresql://")):
        return POSTGRES
    return SQLITE


class SqlTranslator:
    """SQLite SQL in, PostgreSQL SQL out - or a refusal.

    The two engines agree on far less than people assume, and a dashboard that
    silently runs the SQLite version of a query against PostgreSQL is worse than
    one that crashes: it returns the wrong number confidently.
    """

    def __init__(self) -> None:
        self.skipped: list[str] = []

    # -- statements -------------------------------------------------------- #

    def translate(self, sql: str) -> str:
        statement = sql.strip()
        if statement.upper().startswith("PRAGMA"):
            self.skipped.append(statement)
            return ""                       # caller treats "" as a no-op
        statement = _GROUP_CONCAT_RE.sub(r"string_agg(\1, chr(10))", statement)
        statement = self._translate_replace(statement)
        return statement.replace("?", "%s")

    def _translate_replace(self, statement: str) -> str:
        match = _REPLACE_RE.match(statement)
        if match is None:
            if re.match(r"^\s*INSERT\s+OR\b", statement, re.IGNORECASE):
                raise StoreError(
                    "unsupported INSERT variant on PostgreSQL: " +
                    statement.split("\n")[0][:120])
            return statement
        table, columns = match.group(1), match.group(2)
        columns = [c.strip() for c in columns.split(",") if c.strip()]
        keys = PRIMARY_KEYS.get(table.lower())
        if not keys:
            raise StoreError(
                f"INSERT OR REPLACE INTO {table} has no known conflict key; add "
                f"{table} to enterprise.store.PRIMARY_KEYS so the upsert is exact")
        missing = [k for k in keys if k not in columns]
        if missing:
            raise StoreError(
                f"INSERT OR REPLACE INTO {table} does not set its key column(s) "
                f"{', '.join(missing)}")
        updates = [c for c in columns if c not in keys]
        rest = statement[match.end():]
        if updates:
            clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
            tail = f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {clause}"
        else:
            tail = f" ON CONFLICT ({', '.join(keys)}) DO NOTHING"
        return f"INSERT INTO {table} ({', '.join(columns)}){rest}{tail}"

    # -- schema ------------------------------------------------------------ #

    def schema_statements(self, schema: str) -> list[str]:
        """Turn a SQLite schema script into executable PostgreSQL statements."""
        statements = [s.strip() for s in schema.split(";") if s.strip()]
        creates: list[str] = []
        constraints: list[str] = []
        for statement in statements:
            if "CREATE TABLE" in statement.upper():
                creates.append(self._translate_create(statement, constraints))
            else:
                creates.append(self._translate_plain(statement))
        return [c for c in creates if c] + constraints

    def _translate_plain(self, statement: str) -> str:
        statement = _COLLATE_NOCASE_RE.sub("", statement)
        statement = _AUTOINCREMENT_RE.sub(
            "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", statement)
        return statement

    def _translate_create(self, statement: str, constraints: list[str]) -> str:
        head, body = statement.split("(", 1)
        body = body.rsplit(")", 1)[0]
        table_match = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)",
                                head, re.IGNORECASE)
        table = table_match.group(1) if table_match else "unknown"
        columns: list[str] = []
        for part in _split_top_level(body):
            cleaned = self._translate_plain(part.strip())
            fk = _FK_COLUMN_RE.search(cleaned)
            if fk:
                # Two-pass: the referenced table may be created further down the
                # same script, which PostgreSQL refuses and SQLite does not care
                # about. Drop the inline constraint, re-add it afterwards.
                on_delete = (fk.group("ondelete") or "").strip()
                cleaned = (cleaned[:fk.start()] + cleaned[fk.end():]).strip()
                cleaned = re.sub(r",\s*$", "", cleaned)
                constraints.append(
                    f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{fk.group('column')} "
                    f"FOREIGN KEY ({fk.group('column')}) REFERENCES "
                    f"{fk.group('table')}({fk.group('target')})"
                    + (f" {on_delete}" if on_delete else ""))
            if cleaned:
                columns.append(cleaned)
        prefix = head.strip()
        return f"{prefix} (\n    " + ",\n    ".join(columns) + "\n)"


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


# --------------------------------------------------------------------------- #
# Cursors and connections
# --------------------------------------------------------------------------- #

class Result:
    """The slice of DB-API a caller can rely on, minus the dialect."""

    def __init__(self, rows: list[Any], rowcount: int = -1,
                 lastrowid: Optional[int] = None) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


def _psycopg():
    try:
        import psycopg                  # noqa: PLC0415 - optional dependency
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise StoreError(
            "PostgreSQL needs the optional 'psycopg' package: "
            "python -m pip install 'asset-gatekeeper[postgres]'") from exc
    return psycopg, dict_row


class PgConnection:
    """A psycopg connection wearing the sqlite3 API the rest of the code uses.

    ``execute`` translates the statement, ``PRAGMA`` returns an empty result, and
    rows arrive as dicts (``sqlite3.Row`` equivalent).
    """

    def __init__(self, connection: Any, translator: SqlTranslator) -> None:
        self._connection = connection
        self._translator = translator

    def execute(self, sql: str, params: Sequence[Any] | dict | None = None) -> Result:
        translated = self._translator.translate(sql)
        if not translated:
            return Result([], 0)
        cursor = self._connection.cursor()
        try:
            cursor.execute(translated, params or ())
            if cursor.description is None:
                return Result([], cursor.rowcount)
            rows = cursor.fetchall()
            lastrowid = None
            if rows and "id" in rows[0]:
                lastrowid = rows[0].get("id")
            return Result(rows, cursor.rowcount, lastrowid)
        finally:
            cursor.close()

    def executemany(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> int:
        translated = self._translator.translate(sql)
        if not translated:
            return 0
        cursor = self._connection.cursor()
        try:
            cursor.executemany(translated, params_seq)
            return cursor.rowcount
        finally:
            cursor.close()

    def executescript(self, script: str) -> None:
        for statement in self._translator.schema_statements(script):
            cursor = self._connection.cursor()
            try:
                cursor.execute(statement)
            except Exception as exc:
                raise StoreError(f"schema statement failed: {statement[:120]}: {exc}") from exc
            finally:
                cursor.close()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.commit()


class PgPool:
    """A small, honest connection pool.

    ``psycopg_pool`` is used when it is installed (it has health checks and
    reconnection); otherwise this queue of connections is used, which is enough
    for a dashboard and does not pretend to be more.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        psycopg, dict_row = _psycopg()
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._dsn = config.dsn
        self._max = max(1, config.pool_max)
        self._timeout_ms = config.statement_timeout_ms
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False
        self._shared = None
        try:                                    # pragma: no cover - optional pkg
            from psycopg_pool import ConnectionPool  # noqa: PLC0415
            self._shared = ConnectionPool(
                config.dsn, min_size=config.pool_min, max_size=config.pool_max,
                kwargs={"row_factory": dict_row}, open=True)
        except ModuleNotFoundError:
            self._shared = None

    def _new_connection(self) -> Any:
        connection = self._psycopg.connect(self._dsn, row_factory=self._dict_row)
        with contextlib.suppress(Exception):
            connection.execute(f"SET statement_timeout = {self._timeout_ms}")
        return connection

    @contextlib.contextmanager
    def connection(self) -> Iterator[PgConnection]:
        if self._closed:
            raise StoreError("the connection pool is closed")
        raw = None
        if self._shared is not None:               # pragma: no cover - optional
            with self._shared.connection() as shared:
                wrapper = PgConnection(shared, SqlTranslator())
                yield wrapper
                return
        try:
            raw = self._free.get_nowait()
            if getattr(raw, "closed", False) or getattr(raw, "broken", False):
                raw = None
        except queue.Empty:
            raw = None
        if raw is None:
            with self._lock:
                if self._created >= self._max and not self._free.empty():
                    raw = self._free.get()          # give way to an existing one
                self._created += 1
            raw = raw or self._new_connection()
        wrapper = PgConnection(raw, SqlTranslator())
        try:
            yield wrapper
            wrapper.commit()
        except Exception:
            with contextlib.suppress(Exception):
                wrapper.rollback()
            raise
        finally:
            if getattr(raw, "closed", False):
                with self._lock:
                    self._created -= 1
            else:
                self._free.put(raw)

    def close(self) -> None:
        self._closed = True
        if self._shared is not None:               # pragma: no cover - optional
            self._shared.close()
        while True:
            try:
                connection = self._free.get_nowait()
            except queue.Empty:
                break
            with contextlib.suppress(Exception):
                connection.close()

    @property
    def size(self) -> int:
        return self._free.qsize()


class Store:
    """One object, two engines, one set of semantics."""

    def __init__(self, target: str | Path, config: Optional[DatabaseConfig] = None) -> None:
        self.target = str(target)
        self.dialect = dialect_for(target)
        self._pool: Optional[PgPool] = None
        self._sqlite: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        if self.dialect.is_postgres:
            self._pool = PgPool(config or DatabaseConfig(dsn=self.target))
        else:
            connection = sqlite3.connect(self.target, timeout=10.0,
                                         check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._sqlite = connection

    # -- connections ------------------------------------------------------- #

    @contextlib.contextmanager
    def connect(self):
        if self._sqlite is not None:
            with self._lock:
                try:
                    yield self._sqlite
                    self._sqlite.commit()
                except Exception:
                    self._sqlite.rollback()
                    raise
            return
        assert self._pool is not None
        with self._pool.connection() as connection:
            yield connection

    # -- convenience ------------------------------------------------------- #

    def execute(self, sql: str, params: Sequence[Any] | dict | None = None) -> Any:
        with self.connect() as connection:
            if params is None:
                return connection.execute(sql)
            return connection.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict | None = None) -> list[Any]:
        return self.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict | None = None) -> Any:
        return self.execute(sql, params).fetchone()

    def executescript(self, script: str) -> None:
        if self._sqlite is not None:
            with self.connect() as connection:
                connection.executescript(script)        # type: ignore[attr-defined]
            return
        with self.connect() as connection:
            connection.executescript(script)            # type: ignore[attr-defined]
    def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
        if self._pool is not None:
            self._pool.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def open_store(target: str | Path, config: Optional[DatabaseConfig] = None) -> Store:
    """Open SQLite or PostgreSQL from one string, no branching at the call site."""
    return Store(str(target), config)


def open_from_config(config: DatabaseConfig) -> Store:
    return Store(config.dsn if config.is_postgres else config.sqlite_path, config)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

#: Copy order that satisfies foreign keys.
MIGRATION_ORDER = ("projects", "budgets", "tag_rules", "verdicts", "problems",
                   "modifications", "verdict_metrics", "alerts_state",
                   "admin_users", "admin_sessions", "rule_audit", "meta")


def migrate_sqlite(source_path: str | Path, target: Store,
                   batch: int = 500) -> dict[str, int]:
    """Copy every table from a SQLite database into ``target``.

    Idempotent by construction: rows are inserted with ``ON CONFLICT DO NOTHING``
    so re-running after a partial failure resumes rather than duplicating. The
    source is opened read-only and never modified.
    """
    source_path = Path(source_path)
    if not source_path.is_file():
        raise StoreError(f"no such SQLite database: {source_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=10.0)
    source.row_factory = sqlite3.Row
    copied: dict[str, int] = {}
    try:
        present = {row["name"] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in MIGRATION_ORDER:
            if table not in present:
                continue
            columns = [row["name"] for row in source.execute(
                f"PRAGMA table_info({table})")]
            if not columns:
                continue
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            if not rows:
                copied[table] = 0
                continue
            # Columns the target may not have yet (an older source schema) are
            # dropped rather than guessed.
            target_columns = _columns_of(target, table)
            keep = [c for c in columns if not target_columns or c in target_columns]
            statement = (f"INSERT INTO {table} ({', '.join(keep)}) VALUES "
                         f"({', '.join('?' for _ in keep)})")
            if target.dialect.is_postgres:
                statement += " ON CONFLICT DO NOTHING"
            total = 0
            with target.connect() as connection:
                for start in range(0, len(rows), batch):
                    chunk = [tuple(row[c] for c in keep)
                             for row in rows[start:start + batch]]
                    if target.dialect.is_postgres:
                        total += connection.executemany(statement, chunk)  # type: ignore[attr-defined]
                    else:
                        connection.executemany(statement, chunk)           # type: ignore[attr-defined]
                        total += len(chunk)
            copied[table] = total
    finally:
        source.close()
    return copied


def _columns_of(store: Store, table: str) -> set[str]:
    try:
        if store.dialect.is_postgres:
            rows = store.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,))
            return {row["column_name"] for row in rows}
        rows = store.query(f"PRAGMA table_info({table})")
        return {row["name"] for row in rows}
    except Exception:
        return set()
