-- 0003 exceptions: expiring per-project budget exceptions (G1).
--
-- The server's own schema has had this table since exceptions shipped, but the
-- baseline migration did not, so a PostgreSQL database built only from
-- migrations had nowhere to store an exception, and `migrate --sqlite` did not
-- copy them (found on 6 Nov: an exception added in the budget planner did not
-- reach PostgreSQL).
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
