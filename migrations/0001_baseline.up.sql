-- 0001 baseline: the AssetHealth schema as shipped with v1.0.
-- Generated from server/asset_health_server.py SCHEMA; a test asserts
-- that the two still cover the same tables.

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
    passed      INTEGER NOT NULL,
    metric      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'PASSED',
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS problems (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdicts(id) ON DELETE CASCADE,
    message    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS modifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdicts(id) ON DELETE CASCADE,
    action     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT '',
    disabled      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    csrf       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rule_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    user_id    INTEGER,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT '',
    target     TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    old_value  TEXT NOT NULL DEFAULT '',
    new_value  TEXT NOT NULL DEFAULT '',
    source_ip  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS project_roots (
    path      TEXT PRIMARY KEY,
    label     TEXT NOT NULL DEFAULT '',
    last_used TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    api_token  TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL
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

CREATE TABLE IF NOT EXISTS alerts_state (
    scope_key     TEXT PRIMARY KEY,
    last_alert_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verdicts_passed    ON verdicts(passed);

CREATE INDEX IF NOT EXISTS idx_verdicts_timestamp ON verdicts(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_problems_verdict   ON problems(verdict_id);

CREATE INDEX IF NOT EXISTS idx_mods_verdict       ON modifications(verdict_id);

CREATE INDEX IF NOT EXISTS idx_rule_audit_when ON rule_audit(changed_at);
