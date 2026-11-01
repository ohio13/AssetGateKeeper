-- 0002 verdict metrics: a numeric series for polygon growth.
--
-- The verdict table stores a human-readable metric string ("12,345 tris / 3
-- mat"). Parsing that at query time still works for history that predates this
-- table, but a real trend deserves numbers a client cannot mistype.
CREATE TABLE IF NOT EXISTS verdict_metrics (
    verdict_id INTEGER NOT NULL REFERENCES verdicts(id) ON DELETE CASCADE,
    metric     TEXT NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY (verdict_id, metric)
);

CREATE INDEX IF NOT EXISTS idx_metrics_metric ON verdict_metrics(metric, value);
