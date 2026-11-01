-- 0001 down: remove what the baseline created.
-- Reverse creation order, so no foreign key blocks its own drop.

DROP TABLE IF EXISTS alerts_state;
DROP TABLE IF EXISTS budgets;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS project_roots;
DROP TABLE IF EXISTS rule_audit;
DROP TABLE IF EXISTS admin_sessions;
DROP TABLE IF EXISTS admin_users;
DROP TABLE IF EXISTS meta;
DROP TABLE IF EXISTS modifications;
DROP TABLE IF EXISTS problems;
DROP TABLE IF EXISTS verdicts;
