"""Versioned schema migrations that will not block a live cluster.

Shape
    ``migrations/0001_baseline.up.sql`` / ``.down.sql`` - ordered, versioned,
    checksummed. The applied set lives in ``schema_migrations`` with the checksum
    that was applied, so an edited history is detected instead of silently
    re-run.

Zero downtime is a *rule*, not a hope
    :func:`lint` refuses DDL that takes an exclusive lock on a busy table
    (dropping a column, changing a type, renaming, ``SET NOT NULL``), and the
    runner rewrites what it can do safely:

    * ``CREATE INDEX`` becomes ``CREATE INDEX CONCURRENTLY`` on PostgreSQL, which
      also makes that migration non-transactional automatically;
    * ``ADD CONSTRAINT ... FOREIGN KEY`` gains ``NOT VALID``, so the table is not
      scanned under an exclusive lock (a later migration validates it).

    ``SET lock_timeout`` is issued before every migration: if the lock cannot be
    had in two seconds, the migration fails fast instead of queueing every
    writer behind it - the difference between a retry and an outage.

Determinism
    Same files, same order, same checksums, same result. No timestamps in the
    SQL, no ``NOW()`` defaults that differ per run, and a refusal to apply a
    migration whose recorded checksum no longer matches the file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .store import Store, StoreError, connection_is_sqlite

FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.(?P<kind>up|down)\.sql$")
NONTRANSACTIONAL_MARKER = "-- agk:nontransactional"
ALLOW_BLOCKING_MARKER = "-- agk:allow-blocking"

#: Statements that take an ACCESS EXCLUSIVE lock (or rewrite the table) and are
#: therefore refused while a cluster is serving traffic.
BLOCKING_PATTERNS = (
    (re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE), "DROP COLUMN rewrites and locks the table"),
    (re.compile(r"\bDROP\s+TABLE\b(?!\s+IF\s+EXISTS)", re.IGNORECASE), "DROP TABLE removes data for everyone"),
    (re.compile(r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b", re.IGNORECASE), "changing a column type rewrites the table"),
    (re.compile(r"\bRENAME\s+(COLUMN|TO)\b", re.IGNORECASE), "renaming breaks clients mid-flight"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE), "SET NOT NULL scans the table under a lock"),
    (re.compile(r"\bADD\s+CONSTRAINT\b(?![\s\S]*\bNOT\s+VALID)", re.IGNORECASE),
     "ADD CONSTRAINT without NOT VALID validates against a lock"),
    (re.compile(r"\bCREATE\s+INDEX\b(?![\s\S]*CONCURRENTLY)", re.IGNORECASE),
     "CREATE INDEX without CONCURRENTLY blocks writes"),
    (re.compile(r"\bTRUNCATE\b", re.IGNORECASE), "TRUNCATE is instant data loss"),
)

SCHEMA_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations (\n"
    "    version     INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    name        TEXT NOT NULL,\n"
    "    checksum    TEXT NOT NULL,\n"
    "    applied_at  TEXT NOT NULL,\n"
    "    duration_ms INTEGER NOT NULL DEFAULT 0,\n"
    "    kind        TEXT NOT NULL DEFAULT 'up'\n"
    ")")


class MigrationError(Exception):
    """The migration history or a migration file is unusable."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str
    path: Path

    @property
    def key(self) -> str:
        return f"{self.version:04d}_{self.name}"

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.up.strip().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.down.strip().encode("utf-8"))
        return digest.hexdigest()

    @property
    def nontransactional(self) -> bool:
        return NONTRANSACTIONAL_MARKER in self.up or NONTRANSACTIONAL_MARKER in self.down

    @property
    def allows_blocking(self) -> bool:
        return ALLOW_BLOCKING_MARKER in self.up or ALLOW_BLOCKING_MARKER in self.down


@dataclass
class Step:
    key: str
    version: int
    statements: list[str]
    nontransactional: bool
    direction: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "version": self.version, "direction": self.direction,
                "statements": len(self.statements), "nontransactional": self.nontransactional}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def discover(directory: str | Path) -> list[Migration]:
    """Read every migration, refusing gaps, duplicates and half-pairs."""
    root = Path(directory)
    if not root.is_dir():
        raise MigrationError(f"no migration directory at {root}")
    pairs: dict[int, dict[str, Path]] = {}
    for item in sorted(root.iterdir()):
        if not item.is_file():
            continue
        match = FILENAME_RE.match(item.name)
        if match is None:
            if item.suffix == ".sql":
                raise MigrationError(
                    f"{item.name} does not follow NNNN_name.up.sql / .down.sql")
            continue
        version = int(match.group("version"))
        pairs.setdefault(version, {})[match.group("kind")] = item
        pairs[version]["name"] = Path(match.group("name"))
    migrations: list[Migration] = []
    for version in sorted(pairs):
        entry = pairs[version]
        if "up" not in entry or "down" not in entry:
            raise MigrationError(
                f"migration {version:04d} needs both .up.sql and .down.sql "
                f"(missing {'.up' if 'up' not in entry else '.down'})")
        name = entry["name"].name
        migrations.append(Migration(
            version=version, name=name,
            up=entry["up"].read_text(encoding="utf-8-sig"),
            down=entry["down"].read_text(encoding="utf-8-sig"),
            path=entry["up"]))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration versions")
    for expected, actual in enumerate(versions, start=versions[0] if versions else 1):
        if expected != actual:
            raise MigrationError(
                f"migration versions must be contiguous: expected {expected:04d}, "
                f"found {actual:04d}")
    return migrations


# --------------------------------------------------------------------------- #
# Linting and dialect rewriting
# --------------------------------------------------------------------------- #

def strip_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        marker = line.find("--")
        if marker >= 0:
            line = line[:marker]
        if line.strip():
            lines.append(line.rstrip())
    return "\n".join(lines)


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that are not inside a string literal."""
    statements, current, in_string = [], [], False
    for char in strip_comments(sql):
        if char == "'":
            in_string = not in_string
        if char == ";" and not in_string:
            text = "".join(current).strip()
            if text:
                statements.append(text)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def rewrite_for_postgres(statement: str) -> str:
    """Make the statement non-blocking where PostgreSQL allows it."""
    text = statement.strip()
    if re.match(r"^CREATE\s+INDEX\b", text, re.IGNORECASE) and "CONCURRENTLY" not in text.upper():
        text = re.sub(r"^CREATE\s+INDEX\b", "CREATE INDEX CONCURRENTLY", text,
                      count=1, flags=re.IGNORECASE)
    if (re.search(r"^ALTER\s+TABLE\b", text, re.IGNORECASE)
            and re.search(r"\bADD\s+CONSTRAINT\b", text, re.IGNORECASE)
            and "NOT VALID" not in text.upper()):
        text = text.rstrip().rstrip(";") + " NOT VALID"
    return text


def _created_table(statement: str) -> str:
    match = re.match(r"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?([A-Za-z_][A-Za-z0-9_]*)",
                     statement, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def order_statements(statements: list[str]) -> list[str]:
    """Put tables before the tables they reference, and constraints last.

    PostgreSQL checks a foreign key when the table is created, so the baseline's
    ``verdicts`` (which references ``projects``) failed on a fresh server while
    SQLite accepted it - SQLite defers the check, which is exactly why no test
    noticed. The file's own order is not authoritative: this is.

    Translation also *moves* foreign keys into ``ALTER TABLE ... ADD CONSTRAINT``
    statements, and those must run after every table exists, not after their own.
    """
    tables: dict[str, str] = {}
    order: list[str] = []
    others: list[str] = []
    constraints: list[str] = []

    for statement in statements:
        head = statement.lstrip().upper()
        if head.startswith("CREATE TABLE"):
            name = _created_table(statement)
            tables[name] = statement
            order.append(name)
        elif head.startswith("ALTER TABLE") and "ADD CONSTRAINT" in head:
            constraints.append(statement)
        else:
            others.append(statement)

    # Kahn's algorithm, stable: a table waits for the tables it references.
    waiting = {name: {dep for dep in re.findall(r"REFERENCES\s+[\"']?([A-Za-z_][A-Za-z0-9_]*)",
                                                tables[name], re.IGNORECASE)
                      if dep.lower() in tables and dep.lower() != name}
               for name in order}
    emitted: list[str] = []
    while waiting:
        ready = [name for name in order if name in waiting and not waiting[name]]
        if not ready:                     # a cycle: emit what is left in file order
            ready = [name for name in order if name in waiting]
        for name in ready:
            emitted.append(tables.pop(name))
            waiting.pop(name)
        for dependencies in waiting.values():
            dependencies.difference_update(ready)

    return emitted + others + constraints


def effective_statements(sql: str, dialect: str) -> list[str]:
    """The statements that will actually run, after rewriting for the engine.

    Shared by the runner and the linter so "what we checked" and "what we
    executed" cannot drift apart - the linter must judge the rewritten SQL, not
    the author's original.
    """
    statements = split_statements(sql)
    if dialect != "postgres":
        # Ordering is engine-agnostic and SQLite tolerates either, but emitting a
        # table before the table it points at is a trap for the next reader.
        return order_statements(statements)
    from .store import SqlTranslator

    translator = SqlTranslator()
    result: list[str] = []
    for statement in statements:
        if statement.upper().startswith("CREATE TABLE"):
            result.extend(translator.schema_statements(statement))
        else:
            result.append(statement)
    # Rewrite *after* translation as well: the translator generates the
    # ADD CONSTRAINT statements for moved foreign keys, and those need NOT VALID
    # just as much as a hand-written one.
    return [rewrite_for_postgres(statement) for statement in order_statements(result)]


def lint(migration: Migration, dialect: str = "postgres",
         allow_blocking: bool = False) -> list[str]:
    """Problems that would take the cluster out of service during a rollout."""
    problems: list[str] = []
    body = "\n".join(effective_statements(migration.up, dialect))
    for pattern, reason in BLOCKING_PATTERNS:
        if dialect != "postgres" and "CONCURRENTLY" in reason:
            continue                      # SQLite has no concurrent DDL to require
        if pattern.search(body):
            if allow_blocking or migration.allows_blocking:
                continue
            problems.append(f"{migration.key}: {reason}")
    if dialect == "postgres" and migration.nontransactional and "CONCURRENTLY" not in body.upper():
        problems.append(f"{migration.key}: marked non-transactional but contains no "
                        "CONCURRENTLY statement; that flag is only needed for those")
    return problems


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

class MigrationRunner:
    """Applies and rolls back migrations on SQLite or PostgreSQL.

    The same code path drives both: SQLite gets the raw statements, PostgreSQL
    gets them translated (identity columns, two-pass foreign keys) and rewritten
    to be non-blocking.
    """

    def __init__(self, store: Store, directory: str | Path = "migrations",
                 lock_timeout_ms: int = 2000, allow_blocking: bool = False,
                 log: Optional[Any] = None,
                 clock: Any = None) -> None:
        self.store = store
        self.directory = Path(directory)
        self.lock_timeout_ms = int(lock_timeout_ms)
        self.allow_blocking = allow_blocking
        self.log = log
        if clock is None:
            from datetime import datetime, timezone

            def clock() -> str:
                return datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._clock = clock

    # -- helpers ----------------------------------------------------------- #

    @property
    def dialect(self) -> str:
        return self.store.dialect.name

    def _statements_for(self, sql: str) -> list[str]:
        return effective_statements(sql, self.dialect)

    def ensure_table(self) -> None:
        with self.store.connect() as connection:
            if self.dialect == "postgres":
                from .store import SqlTranslator

                for statement in SqlTranslator().schema_statements(SCHEMA_TABLE_SQL):
                    connection.execute(statement)          # type: ignore[attr-defined]
            else:
                connection.execute(SCHEMA_TABLE_SQL)   # type: ignore[attr-defined]

    def applied(self) -> dict[int, dict]:
        self.ensure_table()
        rows = self.store.query(
            "SELECT version, name, checksum, applied_at, duration_ms, kind "
            "FROM schema_migrations ORDER BY version")
        return {int(row["version"]): dict(row) for row in rows}

    # -- verification ------------------------------------------------------ #

    def verify(self) -> list[str]:
        """Everything wrong with the history, as sentences an operator can act on."""
        problems: list[str] = []
        migrations = discover(self.directory)
        applied = self.applied()
        known = {m.version: m for m in migrations}
        for version, row in applied.items():
            migration = known.get(version)
            if migration is None:
                problems.append(f"{version:04d} is applied but no file exists "
                                "(a migration was deleted from history)")
                continue
            if row["checksum"] != migration.checksum:
                problems.append(
                    f"{migration.key}: checksum changed since it was applied "
                    f"({row['checksum'][:12]} -> {migration.checksum[:12]}); never "
                    "edit an applied migration, add a new one")
        for version, migration in known.items():
            if version not in applied:
                problems.extend(lint(migration, self.dialect, self.allow_blocking))
        return problems

    def plan(self) -> list[Migration]:
        applied = self.applied()
        return [m for m in discover(self.directory) if m.version not in applied]

    def stamp_all(self) -> list[str]:
        """Record every pending migration as applied without running it.

        For a database just built from the server's full schema (``migrate
        --sqlite --init-schema``), which already contains everything the
        migrations would create. Returns the keys recorded."""
        pending = self.plan()
        if not pending:
            return []
        stamped_at = self._clock()
        with self.store.connect() as connection:
            for migration in pending:
                connection.execute(                              # type: ignore[attr-defined]
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at,"
                    " duration_ms, kind) VALUES (?, ?, ?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, stamped_at, 0,
                     "stamp"))
        return [migration.key for migration in pending]

    # -- execution --------------------------------------------------------- #

    def _lock(self, connection: Any) -> bool:
        if self.dialect != "postgres":
            return False
        try:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(?) AS locked", (781_004,)).fetchone()
            return bool(row["locked"]) if row else False
        except Exception as exc:                     # pragma: no cover - permission
            raise MigrationError(f"cannot take the migration lock: {exc}") from exc

    def _unlock(self, connection: Any) -> None:
        if self.dialect != "postgres":
            return
        try:
            connection.execute("SELECT pg_advisory_unlock(?)", (781_004,))
        except Exception:                            # pragma: no cover
            pass

    def apply(self, to: Optional[int] = None, dry_run: bool = False) -> list[dict]:
        problems = self.verify()
        if problems:
            raise MigrationError("; ".join(problems))
        pending = [m for m in self.plan() if to is None or m.version <= to]
        done: list[dict] = []
        if not pending:
            return done
        with self.store.connect() as connection:
            previous_autocommit = self._enter_autocommit(connection)
            if not self._lock(connection) and self.dialect == "postgres":
                self._leave_autocommit(connection, previous_autocommit)
                raise MigrationError("another migration is running (advisory lock held)")
            try:
                if self.dialect == "postgres":
                    connection.execute(                          # type: ignore[attr-defined]
                        f"SET lock_timeout = {self.lock_timeout_ms}")
                for migration in pending:
                    statements = self._statements_for(migration.up)
                    step = Step(migration.key, migration.version, statements,
                                migration.nontransactional, "up")
                    if dry_run:
                        done.append(step.as_dict())
                        continue
                    import time as _time

                    started = self._clock()
                    began = _time.perf_counter()
                    self._run_step(
                        connection, statements,
                        "INSERT INTO schema_migrations (version, name, checksum,"
                        " applied_at, duration_ms, kind) VALUES (?, ?, ?, ?, ?, ?)",
                        lambda: (migration.version, migration.name, migration.checksum,
                                 started, int((_time.perf_counter() - began) * 1000), "up"))
                    done.append(step.as_dict())
                    if self.log is not None:
                        self.log.emit("migration.applied", actor="agk-enterprise",
                                      action="schema.up", outcome="applied",
                                      detail={"migration": migration.key,
                                              "statements": len(statements)})
            finally:
                self._unlock(connection)
                self._leave_autocommit(connection, previous_autocommit)
        return done

    def rollback(self, to: int = 0, dry_run: bool = False) -> list[dict]:
        """Apply ``.down`` scripts newest-first until ``to`` (exclusive)."""
        applied = self.applied()
        known = {m.version: m for m in discover(self.directory)}
        targets = [known[v] for v in sorted(applied, reverse=True)
                   if v > to and v in known]
        done: list[dict] = []
        if not targets:
            return done
        with self.store.connect() as connection:
            previous_autocommit = self._enter_autocommit(connection)
            if not self._lock(connection) and self.dialect == "postgres":
                self._leave_autocommit(connection, previous_autocommit)
                raise MigrationError("another migration is running (advisory lock held)")
            try:
                for migration in targets:
                    statements = self._statements_for(migration.down)
                    step = Step(migration.key, migration.version, statements,
                                migration.nontransactional, "down")
                    if dry_run:
                        done.append(step.as_dict())
                        continue
                    self._run_step(connection, statements,
                                   "DELETE FROM schema_migrations WHERE version = ?",
                                   lambda: (migration.version,))
                    done.append(step.as_dict())
                    if self.log is not None:
                        self.log.emit("migration.applied", actor="agk-enterprise",
                                      action="schema.down", outcome="rolled_back",
                                      detail={"migration": migration.key})
            finally:
                self._unlock(connection)
                self._leave_autocommit(connection, previous_autocommit)
        return done

    # -- running one step ---------------------------------------------------- #

    def _enter_autocommit(self, connection: Any) -> Optional[bool]:
        """PostgreSQL: run the session in autocommit so each step picks its mode."""
        if self.dialect != "postgres" or not hasattr(connection, "set_autocommit"):
            return None
        return connection.set_autocommit(True)

    @staticmethod
    def _leave_autocommit(connection: Any, previous: Optional[bool]) -> None:
        if previous is None:
            return
        try:
            connection.set_autocommit(previous)
        except Exception:                            # pragma: no cover
            pass

    def _run_step(self, connection: Any, statements: list[str], record_sql: str,
                  record_params) -> None:
        """Run one migration's statements, then record it.

        PostgreSQL refuses ``CREATE INDEX CONCURRENTLY`` inside a transaction
        block, and the runner rewrites every ``CREATE INDEX`` that way - so a
        step containing one runs statement by statement in autocommit (the
        record is written last: a failure leaves the step unrecorded, and
        re-running it is safe because tables and indexes use IF NOT EXISTS and
        moved constraints are skipped when present). Every other PostgreSQL
        step is one explicit transaction. SQLite is unchanged.
        """
        concurrent = any("CONCURRENTLY" in statement.upper() for statement in statements)
        if self.dialect == "postgres" and hasattr(connection, "transaction") and not concurrent:
            with connection.transaction():
                for statement in statements:
                    connection.execute(statement)
                connection.execute(record_sql, record_params())
            return
        for statement in statements:
            connection.execute(statement)                # type: ignore[attr-defined]
        connection.execute(record_sql, record_params())  # type: ignore[attr-defined]

    # -- verification of the *result* ------------------------------------- #

    def check_tables(self, expected: Iterable[str]) -> list[str]:
        """After a rollout, prove the tables the code needs are actually there."""
        missing = []
        for table in expected:
            try:
                if self.dialect == "postgres":
                    rows = self.store.query(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = ?", (table,))
                else:
                    rows = self.store.query(f"PRAGMA table_info({table})")
            except StoreError as exc:                # pragma: no cover
                missing.append(f"{table}: {exc}")
                continue
            if not rows:
                missing.append(f"{table}: table is missing")
        return missing


def baseline_sql(tables: Iterable[str]) -> str:
    """Helper for tests and tooling: a portable ``CREATE TABLE IF NOT EXISTS``."""
    return ";\n".join(f"CREATE TABLE IF NOT EXISTS {name} (id INTEGER PRIMARY KEY AUTOINCREMENT)"
                      for name in tables)


def assert_sqlite_compatible(sql: str) -> None:
    """Guard used by the shipped migrations in CI: no PostgreSQL-only SQL."""
    forbidden = ("CONCURRENTLY", "NOT VALID", "::", "SERIAL")
    for token in forbidden:
        if token in sql:
            raise MigrationError(
                f"migration uses PostgreSQL-only syntax ({token}); the runner "
                "rewrites it at apply time instead")


def is_sqlite_store(store: Store) -> bool:
    return store.is_sqlite


def connection_dialect(connection: Any) -> str:
    return "sqlite" if connection_is_sqlite(connection) else "postgres"
