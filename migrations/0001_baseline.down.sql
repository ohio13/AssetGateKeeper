-- 0001 down: remove what the baseline created.
-- Dependents first: a table is dropped only after every table whose foreign
-- key points at it (verdicts -> projects, problems/modifications -> verdicts,
-- admin_sessions -> admin_users, budgets -> projects). SQLite never checked
-- this; PostgreSQL refuses to drop a referenced table.

DROP TABLE IF EXISTS alerts_state;
DROP TABLE IF EXISTS budgets;
DROP TABLE IF EXISTS modifications;
DROP TABLE IF EXISTS problems;
DROP TABLE IF EXISTS verdicts;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS project_roots;
DROP TABLE IF EXISTS rule_audit;
DROP TABLE IF EXISTS admin_sessions;
DROP TABLE IF EXISTS admin_users;
DROP TABLE IF EXISTS meta;
