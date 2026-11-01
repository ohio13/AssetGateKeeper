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
import dataclasses
import queue
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .config import DatabaseConfig
from .resilience import CircuitBreaker, HealthMonitor


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
    r"(?:PRIMARY\s+KEY\s+)?(?P<notnull>NOT\s+NULL\s+)?"
    r"REFERENCES\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?P<target>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
    r"(?P<ondelete>\s+ON\s+DELETE\s+[A-Z ]+?)?(?=\s*,|\s*$)",
    re.IGNORECASE)

#: The inline ``REFERENCES t(c) [ON DELETE ...]`` part of one column definition.
_REFERENCES_CLAUSE_RE = re.compile(
    r"\s+REFERENCES\s+[A-Za-z_][A-Za-z0-9_]*\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)"
    r"(?:\s+ON\s+DELETE\s+(?:SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION))?",
    re.IGNORECASE)

#: ``ALTER TABLE t ADD CONSTRAINT name ...`` - the statements the translator moves
#: foreign keys into. PostgreSQL has no ``ADD CONSTRAINT IF NOT EXISTS``.
_ADD_CONSTRAINT_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s+"
    r"ADD\s+CONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)


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
        # Strip comments *before* splitting on ';'. A semicolon inside a comment
        # ("... on its way in; an ERROR row means ...") split a CREATE TABLE in
        # half and shipped the fragment to the server as a statement.
        statements = [s.strip() for s in _strip_comments(schema).split(";") if s.strip()]
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
                # Remove the REFERENCES clause only. The match above spans the
                # whole column definition, and cutting that out dropped the
                # column itself (``problems.verdict_id``), so the index and
                # the re-added constraint then failed on a column that was gone.
                clause = _REFERENCES_CLAUSE_RE.search(cleaned, fk.start())
                if clause is not None:
                    cleaned = (cleaned[:clause.start()] + cleaned[clause.end():]).strip()
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


def _strip_comments(statement: str) -> str:
    """Drop ``--`` comments, inline and full-line.

    Nothing in a schema string contains a literal ``--`` (no default value
    does), and leaving them in breaks both the split on commas and the split on
    parentheses: the shipped schema has an inline `-- 0/1` and a paragraph of
    prose ending in "(opt-in)" before a CREATE.
    """
    cleaned = []
    for line in statement.splitlines():
        marker = line.find("--")
        if marker >= 0:
            line = line[:marker]
        if line.strip():
            cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip()


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


class Row:
    """A row that reads like ``sqlite3.Row`` *and* like a dict.

    The dashboard was written against sqlite3.Row, which supports both
    ``row["column"]`` and ``row[0]``. PostgreSQL's dict rows only support the
    first, so a handful of positional reads would have raised only in
    production. This wrapper keeps both working and costs one list per row.
    """

    __slots__ = ("_map", "_keys", "_values")

    def __init__(self, values: dict[str, Any]) -> None:
        self._map = dict(values)
        self._keys = list(self._map)
        self._values = [self._map[key] for key in self._keys]

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def keys(self) -> list[str]:
        return list(self._keys)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: Any) -> bool:
        return key in self._map

    def __repr__(self) -> str:
        return f"Row({self._map!r})"


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

    def _constraint_exists(self, statement: str) -> bool:
        """True when ``statement`` adds a constraint that is already there.

        The schema is re-run on every dashboard start and every resumed
        migration, and ``CREATE TABLE IF NOT EXISTS`` is idempotent while the
        moved foreign keys were not: the second start of a PostgreSQL dashboard
        died on "constraint ... already exists".
        """
        match = _ADD_CONSTRAINT_RE.match(statement)
        if match is None:
            return False
        cursor = self._connection.cursor()
        try:
            cursor.execute("SELECT 1 FROM pg_constraint WHERE conname = %s"
                           " AND conrelid = to_regclass(%s)",
                           (match.group(2).lower(), match.group(1).lower()))
            return cursor.fetchone() is not None
        except Exception:
            # No catalog to ask (the test suite's PostgreSQL double): fall back
            # to running the statement and letting the server decide.
            return False
        finally:
            cursor.close()

    def set_autocommit(self, value: bool) -> bool:
        """Switch autocommit, returning the previous setting.

        Needed by the migration runner: ``CREATE INDEX CONCURRENTLY`` is refused
        inside a transaction block, and psycopg opens one implicitly.
        """
        previous = bool(self._connection.autocommit)
        if previous != value:
            if value:
                self._connection.commit()       # close any implicit transaction first
            self._connection.autocommit = value
        return previous

    @contextlib.contextmanager
    def transaction(self) -> Iterator["PgConnection"]:
        """An explicit transaction (commits on success, rolls back on error)."""
        with self._connection.transaction():
            yield self

    def execute(self, sql: str, params: Sequence[Any] | dict | None = None) -> Result:
        translated = self._translator.translate(sql)
        if not translated:
            return Result([], 0)
        if self._constraint_exists(translated):
            return Result([], 0)
        cursor = self._connection.cursor()
        try:
            cursor.execute(translated, params or ())
            if cursor.description is None:
                return Result([], cursor.rowcount)
            rows = [Row(dict(row)) for row in cursor.fetchall()]
            lastrowid = None
            if rows and "id" in rows[0]:
                lastrowid = rows[0]["id"]
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
            if self._constraint_exists(statement):
                continue
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
        # An empty DSN means SQLite (config.py says so), so the *config* decides the
        # engine, not the first argument. ``Store(config)`` used to take the SQLite
        # branch, then create a file named after the config's repr - so a
        # postgresql:// deployment silently wrote to disk and every test passed.
        #
        # An *explicit* target still has to win over that config: the CLI always
        # passes ``config.database`` (empty DSN when there is no enterprise.toml),
        # and letting the config decide sent ``migrate --dsn postgresql://...``
        # and ``trends --db postgresql://...`` to the local asset_health.db while
        # reporting success. The rule is now: a target given here is the address;
        # the config only contributes pool, timeout and replica settings.
        if isinstance(target, DatabaseConfig):
            config, target = (config or target), ""
        explicit = str(target or "").strip()
        base = config or DatabaseConfig()
        if explicit and is_dsn(explicit):
            settings = dataclasses.replace(base, dsn=explicit)
            self.target = explicit
        elif explicit:
            settings = dataclasses.replace(base, dsn="", sqlite_path=explicit)
            self.target = explicit
        else:
            settings = base
            self.target = str(base.dsn if base.is_postgres else base.sqlite_path)
        self.dialect = dialect_for(self.target)
        self._pool: Optional[PgPool] = None
        self._sqlite: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._config = settings
        self._breaker = CircuitBreaker(
            f"primary:{_short(self.target)}",
            failure_threshold=settings.breaker_threshold,
            reset_timeout=settings.breaker_reset_seconds)
        self._replicas: list[tuple[str, "Store", CircuitBreaker]] = []
        self._read_cursor = 0
        self._monitors: list[HealthMonitor] = []
        if self.dialect.is_postgres:
            self._pool = PgPool(settings)
            # Replicas are plain Stores with no replicas of their own: a replica
            # of a replica is a latency bug wearing a feature's clothes.
            for dsn in settings.read_dsns:
                replica_config = DatabaseConfig(
                    dsn=dsn, pool_min=1, pool_max=max(2, settings.pool_max // 2),
                    statement_timeout_ms=settings.statement_timeout_ms,
                    breaker_threshold=settings.breaker_threshold,
                    breaker_reset_seconds=settings.breaker_reset_seconds,
                    health_interval_seconds=0)
                replica = Store(dsn, replica_config)
                self._replicas.append((dsn, replica, replica._breaker))
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

    @contextlib.contextmanager
    def session(self):
        """One short-lived unit of work, the way a request handler wants it.

        SQLite gets a *fresh* connection per unit of work - cheap, and it keeps
        WAL's reader concurrency without sharing a handle across threads (which
        is what v1 did, and what the existing behaviour and tests expect).
        PostgreSQL gets a pooled connection instead, because opening one per
        request would defeat the pool. The caller cannot tell which it received.
        """
        if self._sqlite is not None:
            connection = sqlite3.connect(self.target, timeout=10.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                yield connection
                connection.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    connection.rollback()
                raise
            finally:
                connection.close()
            return
        with self.connect() as connection:
            yield connection

    @property
    def is_sqlite(self) -> bool:
        return self._sqlite is not None

    # -- resilience -------------------------------------------------------- #

    def endpoints(self) -> list[tuple[str, "Store", CircuitBreaker]]:
        """Primary plus replicas, in the order a reader should try them."""
        return [(self.target, self, self._breaker)] + list(self._replicas)

    def ping(self) -> bool:
        """One round trip, used by the health loop."""
        self.query("SELECT 1 AS one")
        return True

    def start_health(self, interval: Optional[float] = None) -> "Store":
        """Start a ping loop per PostgreSQL endpoint.

        Off unless asked for: a library that starts threads on construction is a
        library that hangs test suites. The server calls this once at startup.
        """
        if not self.dialect.is_postgres or self._monitors:
            return self
        interval = self._config.health_interval_seconds if interval is None else interval
        if not interval:
            return self
        for name, endpoint, breaker in self.endpoints():
            monitor = HealthMonitor(endpoint.ping, name=_short(name),
                                    interval=interval, breaker=breaker)
            monitor.start()
            self._monitors.append(monitor)
        return self

    def stop_health(self) -> None:
        for monitor in self._monitors:
            monitor.stop()
        self._monitors.clear()

    def health(self) -> dict:
        return {
            "dialect": self.dialect.name,
            "endpoints": [
                {"endpoint": _short(name), "breaker": breaker.as_dict(),
                 "health": (self._monitors[index].as_dict()
                            if index < len(self._monitors) else None)}
                for index, (name, _, breaker) in enumerate(self.endpoints())
            ],
        }

    def reader(self) -> "ReadView":
        """A read-only view that prefers a replica and falls back to the primary."""
        if not self._replicas:
            return ReadView(self, None)
        return ReadView(self, self)

    def _next_replica(self) -> Optional[tuple[str, "Store", CircuitBreaker]]:
        """Round-robin over replicas whose breaker will admit a call."""
        if not self._replicas:
            return None
        with self._lock:
            start = self._read_cursor
            self._read_cursor = (self._read_cursor + 1) % max(1, len(self._replicas))
        order = self._replicas[start:] + self._replicas[:start]
        for entry in order:
            if entry[2].allow():
                return entry
        return None

    def read(self, sql: str, params: Sequence[Any] | dict | None = None) -> list[Any]:
        """Run a read-only statement against a replica when one is healthy."""
        replica = self._next_replica()
        if replica is not None:
            name, endpoint, breaker = replica
            try:
                rows = endpoint.query(sql, params)
                breaker.record_success()
                return rows
            except Exception as exc:
                breaker.record_failure(exc)
                # Fall through to the primary: a stale read beats no dashboard.
        return self.query(sql, params)

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
        self.stop_health()
        for _, replica, _ in self._replicas:
            with contextlib.suppress(Exception):
                replica.close()
        if self._sqlite is not None:
            self._sqlite.close()
        if self._pool is not None:
            self._pool.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class ReadView:
    """What analytics and audit listings get: reads only, replica-first.

    Writes are refused rather than silently redirected, so a query is never
    accidentally applied to a replica that will not accept it.
    """

    def __init__(self, owner: Store, router: Optional[Store]) -> None:
        self._owner = owner
        self._router = router

    @property
    def dialect(self) -> Dialect:
        return self._owner.dialect

    @property
    def endpoints(self) -> int:
        return len(self._owner._replicas)

    def query(self, sql: str, params: Sequence[Any] | dict | None = None) -> list[Any]:
        if self._router is not None:
            return self._router.read(sql, params)
        return self._owner.query(sql, params)

    def one(self, sql: str, params: Sequence[Any] | dict | None = None) -> Any:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise StoreError("this is a read view: analytics and audit listings never "
                         "write, and a write sent here would go to a replica")


def _short(dsn: str) -> str:
    """A DSN without its credentials, for logs and health output."""
    text = str(dsn)
    if "@" in text and "://" in text:
        scheme, rest = text.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return text if len(text) <= 40 else text[:37] + "..."


def open_store(target: str | Path, config: Optional[DatabaseConfig] = None) -> Store:
    """Open SQLite or PostgreSQL from one string, no branching at the call site."""
    if isinstance(target, DatabaseConfig):
        return Store(target, config)
    return Store(str(target), config)


def is_dsn(target: str | Path) -> bool:
    """Is this a connection URL rather than a file path?"""
    return str(target).lower().startswith(("postgres://", "postgresql://"))


def connection_is_sqlite(connection: Any) -> bool:
    return isinstance(connection, sqlite3.Connection)


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
