-- Additive: replies precomputed by the Actions job (derived data, overwritten
-- in place). The Worker uses a row only while its input fingerprint matches the
-- current data and it is recent enough; otherwise it computes the reply itself.
CREATE TABLE IF NOT EXISTS cloud_precomputed (
    kind TEXT PRIMARY KEY CHECK (kind IN ('recurring','weekly_cmd','recommend','daily_report','weekly_report')),
    period_key TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) BETWEEN 1 AND 512),
    computed_at TEXT NOT NULL,
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 100000)
);
