-- Additive: owner archive/restore events for recurring plans. Rows in
-- cloud_plans and their revisions are never deleted; the latest event per plan
-- decides whether the plan is hidden from listings, totals and reports.
CREATE TABLE IF NOT EXISTS cloud_plan_archive_events (
    event_no INTEGER PRIMARY KEY,
    plan_key TEXT NOT NULL REFERENCES cloud_plans(plan_key),
    action TEXT NOT NULL CHECK (action IN ('ARCHIVE','RESTORE')),
    effective_at TEXT NOT NULL,
    mutation_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS cloud_plan_archive_events_plan
    ON cloud_plan_archive_events(plan_key, event_no);
CREATE TRIGGER IF NOT EXISTS fence_cloud_plan_archive_insert BEFORE INSERT ON cloud_plan_archive_events
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;
CREATE TRIGGER IF NOT EXISTS plan_archive_immutable_update BEFORE UPDATE ON cloud_plan_archive_events
BEGIN SELECT RAISE(ABORT,'plan archive event immutable'); END;
CREATE TRIGGER IF NOT EXISTS plan_archive_immutable_delete BEFORE DELETE ON cloud_plan_archive_events
BEGIN SELECT RAISE(ABORT,'plan archive event immutable'); END;
