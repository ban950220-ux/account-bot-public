-- Additive cloud-only tables. No operating data is included in this file.
CREATE TABLE IF NOT EXISTS cloud_update_claims (
    update_id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    claim_nonce TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_report_claims (
    period_key TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL,
    claim_nonce TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_outbox (
    event_key TEXT PRIMARY KEY,
    destination_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'UNKNOWN')),
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS cloud_outbox_status ON cloud_outbox (status, created_at);

-- Immutable transfer/staging tables. All account references are opaque
-- fingerprints; decimal amounts are TEXT, never SQLite REAL.
CREATE TABLE IF NOT EXISTS cloud_accounts (
    account_fingerprint TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    masked_label TEXT,
    base_currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_plans (
    plan_key TEXT PRIMARY KEY,
    account_fingerprint TEXT REFERENCES cloud_accounts(account_fingerprint),
    source TEXT NOT NULL,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    instrument_currency TEXT NOT NULL,
    amount_currency TEXT NOT NULL,
    daily_amount TEXT NOT NULL,
    target_weight TEXT,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    timezone_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    memo TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    last_mutation_key TEXT
);

CREATE TABLE IF NOT EXISTS cloud_plan_revisions (
    plan_key TEXT NOT NULL REFERENCES cloud_plans(plan_key),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    change_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    account_fingerprint TEXT REFERENCES cloud_accounts(account_fingerprint),
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    instrument_currency TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    daily_amount TEXT NOT NULL,
    amount_currency TEXT NOT NULL,
    target_weight TEXT,
    timezone_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    memo TEXT,
    mutation_key TEXT,
    PRIMARY KEY (plan_key, revision_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS cloud_plan_revision_mutation
    ON cloud_plan_revisions(plan_key, mutation_key);

CREATE TABLE IF NOT EXISTS cloud_mutation_checks (
    mutation_key TEXT NOT NULL,
    plan_key TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK (valid = 1),
    PRIMARY KEY (mutation_key, plan_key)
);

CREATE TABLE IF NOT EXISTS cloud_theses (
    instrument_key TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    currency TEXT NOT NULL,
    current_revision_no INTEGER NOT NULL CHECK (current_revision_no >= 0),
    last_mutation_key TEXT
);

CREATE TABLE IF NOT EXISTS cloud_thesis_revisions (
    instrument_key TEXT NOT NULL REFERENCES cloud_theses(instrument_key),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    revision_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    thesis_text TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    mutation_key TEXT NOT NULL,
    PRIMARY KEY (instrument_key, revision_no),
    UNIQUE (instrument_key, revision_key),
    UNIQUE (instrument_key, mutation_key)
);

CREATE TABLE IF NOT EXISTS cloud_transactions (
    account_fingerprint TEXT NOT NULL REFERENCES cloud_accounts(account_fingerprint),
    source_key TEXT NOT NULL,
    source TEXT NOT NULL,
    market TEXT,
    ticker TEXT,
    asset_type TEXT,
    currency TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    status TEXT NOT NULL,
    traded_at TEXT NOT NULL,
    settled_at TEXT,
    quantity TEXT NOT NULL,
    unit_price TEXT,
    gross_amount TEXT,
    fee TEXT,
    tax TEXT,
    net_amount TEXT,
    PRIMARY KEY (account_fingerprint, source_key)
);

CREATE TABLE IF NOT EXISTS cloud_snapshots (
    idempotency_key TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COLLECTING','COMPLETE','PARTIAL','FAILED')),
    base_currency TEXT NOT NULL,
    total_market_value TEXT,
    total_cost_basis TEXT,
    finalized_at TEXT
);

CREATE TABLE IF NOT EXISTS cloud_snapshot_sources (
    snapshot_key TEXT NOT NULL REFERENCES cloud_snapshots(idempotency_key),
    source_key TEXT NOT NULL,
    account_fingerprint TEXT REFERENCES cloud_accounts(account_fingerprint),
    status TEXT NOT NULL CHECK (status IN
        ('PENDING','SUCCESS','PARTIAL','FAILED','STALE','UNSUPPORTED')),
    observed_at TEXT,
    safe_error_type TEXT,
    PRIMARY KEY (snapshot_key, source_key)
);

CREATE TABLE IF NOT EXISTS cloud_position_snapshots (
    snapshot_key TEXT NOT NULL REFERENCES cloud_snapshots(idempotency_key),
    account_fingerprint TEXT NOT NULL REFERENCES cloud_accounts(account_fingerprint),
    instrument_key TEXT NOT NULL,
    currency TEXT NOT NULL,
    quantity TEXT NOT NULL,
    cost_basis TEXT,
    market_value TEXT,
    value_in_base TEXT,
    PRIMARY KEY (snapshot_key, account_fingerprint, instrument_key)
);

CREATE TABLE IF NOT EXISTS cloud_cash_snapshots (
    snapshot_key TEXT NOT NULL REFERENCES cloud_snapshots(idempotency_key),
    account_fingerprint TEXT NOT NULL REFERENCES cloud_accounts(account_fingerprint),
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    value_in_base TEXT,
    PRIMARY KEY (snapshot_key, account_fingerprint, currency)
);

-- Public market observations only. No account, token, or private snapshot data.
CREATE TABLE IF NOT EXISTS cloud_market_cache (
    provider TEXT NOT NULL,
    namespace TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    stale_until TEXT,
    PRIMARY KEY (provider, namespace, cache_key)
);

-- Append-only owner-supplied normalized documents. No public upload route exists.
CREATE TABLE IF NOT EXISTS cloud_manual_input_revisions (
    source TEXT NOT NULL CHECK (source = 'meritz'),
    kind TEXT NOT NULL CHECK (kind IN ('ACCOUNT_SNAPSHOT', 'RECURRING_PLANS')),
    revision INTEGER NOT NULL,
    mutation_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (source, kind, revision),
    UNIQUE (source, kind, mutation_key)
);

-- Inert legacy archive. The operating Worker never reads these tables for
-- commands, reports, outbox delivery, or recurring execution. It preserves
-- source SQLite identity and schema for separately approved restoration.
CREATE TABLE IF NOT EXISTS cloud_legacy_archive_runs (
    run_id TEXT PRIMARY KEY,
    source_logical_sha256 TEXT NOT NULL,
    source_backup_sha256 TEXT NOT NULL,
    schema_sha256 TEXT NOT NULL,
    table_count INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STAGING','VERIFIED'))
);

CREATE TABLE IF NOT EXISTS cloud_legacy_archive_schema (
    run_id TEXT NOT NULL REFERENCES cloud_legacy_archive_runs(run_id),
    object_type TEXT NOT NULL CHECK (object_type IN ('table','index','view','trigger')),
    object_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    create_sql TEXT NOT NULL,
    PRIMARY KEY (run_id, object_type, object_name)
);

CREATE TABLE IF NOT EXISTS cloud_legacy_archive_rows (
    run_id TEXT NOT NULL REFERENCES cloud_legacy_archive_runs(run_id),
    table_name TEXT NOT NULL,
    row_ordinal INTEGER NOT NULL CHECK (row_ordinal >= 0),
    encoded_values TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, table_name, row_ordinal)
);

CREATE TABLE IF NOT EXISTS cloud_legacy_archive_columns (
    run_id TEXT NOT NULL REFERENCES cloud_legacy_archive_runs(run_id),
    table_name TEXT NOT NULL,
    column_ordinal INTEGER NOT NULL CHECK (column_ordinal >= 0),
    column_name TEXT NOT NULL,
    PRIMARY KEY (run_id, table_name, column_ordinal)
);

-- Resumable uploader bookkeeping. Only VERIFIED runs are visible to readers.
CREATE TABLE IF NOT EXISTS cloud_legacy_archive_imports (
    run_id TEXT PRIMARY KEY REFERENCES cloud_legacy_archive_runs(run_id),
    chunk_root_sha256 TEXT NOT NULL,
    expected_chunks INTEGER NOT NULL CHECK (expected_chunks > 0)
);

CREATE TABLE IF NOT EXISTS cloud_legacy_archive_chunks (
    run_id TEXT NOT NULL REFERENCES cloud_legacy_archive_runs(run_id),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    PRIMARY KEY (run_id, chunk_index)
);

CREATE TRIGGER IF NOT EXISTS cloud_legacy_rows_staging_only
BEFORE INSERT ON cloud_legacy_archive_rows
WHEN (SELECT status FROM cloud_legacy_archive_runs WHERE run_id = NEW.run_id) <> 'STAGING'
BEGIN SELECT RAISE(ABORT, 'archive sealed'); END;

CREATE TRIGGER IF NOT EXISTS cloud_legacy_schema_staging_only
BEFORE INSERT ON cloud_legacy_archive_schema
WHEN (SELECT status FROM cloud_legacy_archive_runs WHERE run_id = NEW.run_id) <> 'STAGING'
BEGIN SELECT RAISE(ABORT, 'archive sealed'); END;

CREATE TRIGGER IF NOT EXISTS cloud_legacy_columns_staging_only
BEFORE INSERT ON cloud_legacy_archive_columns
WHEN (SELECT status FROM cloud_legacy_archive_runs WHERE run_id = NEW.run_id) <> 'STAGING'
BEGIN SELECT RAISE(ABORT, 'archive sealed'); END;

CREATE TRIGGER IF NOT EXISTS cloud_legacy_chunks_staging_only
BEFORE INSERT ON cloud_legacy_archive_chunks
WHEN (SELECT status FROM cloud_legacy_archive_runs WHERE run_id = NEW.run_id) <> 'STAGING'
BEGIN SELECT RAISE(ABORT, 'archive sealed'); END;

-- Inert, resumable core projection and a single explicit cutover fence.
CREATE TABLE IF NOT EXISTS cloud_core_import_runs (
    run_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STAGING','VERIFIED'))
);
CREATE TABLE IF NOT EXISTS cloud_core_import_chunks (
    run_id TEXT NOT NULL REFERENCES cloud_core_import_runs(run_id),
    chunk_index INTEGER NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    items_json TEXT NOT NULL,
    PRIMARY KEY (run_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS cloud_cutover (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    run_id TEXT NOT NULL REFERENCES cloud_core_import_runs(run_id),
    epoch INTEGER NOT NULL CHECK (epoch>0),
    state TEXT NOT NULL CHECK (state IN ('STAGING','VERIFIED','ACTIVE','PAUSED')),
    owner_user_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    scope_fingerprint TEXT,
    source_sha256 TEXT NOT NULL,
    report_timezone TEXT NOT NULL,
  portfolio_id INTEGER NOT NULL CHECK (portfolio_id > 0),
    activated_at TEXT,
    reports_after TEXT,
    edits_enabled INTEGER NOT NULL DEFAULT 0 CHECK (edits_enabled IN (0,1)),
    sends_enabled INTEGER NOT NULL DEFAULT 0 CHECK (sends_enabled IN (0,1)),
    proof_json TEXT
);
CREATE TABLE IF NOT EXISTS cloud_cutover_audit (
    epoch INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    proof_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS core_chunks_sealed_insert BEFORE INSERT ON cloud_core_import_chunks
WHEN (SELECT status FROM cloud_core_import_runs WHERE run_id=NEW.run_id)!='STAGING'
BEGIN SELECT RAISE(ABORT,'core import sealed'); END;
CREATE TRIGGER IF NOT EXISTS core_chunks_sealed_update BEFORE UPDATE ON cloud_core_import_chunks
BEGIN SELECT RAISE(ABORT,'core import chunks immutable'); END;
CREATE TRIGGER IF NOT EXISTS core_chunks_sealed_delete BEFORE DELETE ON cloud_core_import_chunks
BEGIN SELECT RAISE(ABORT,'core import chunks immutable'); END;
CREATE TRIGGER IF NOT EXISTS core_manifest_immutable BEFORE UPDATE OF manifest_json,metadata_json ON cloud_core_import_runs
BEGIN SELECT RAISE(ABORT,'core manifest immutable'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_accounts_insert BEFORE INSERT ON cloud_accounts
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_accounts_update BEFORE UPDATE ON cloud_accounts
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_accounts_delete BEFORE DELETE ON cloud_accounts
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_plans_insert BEFORE INSERT ON cloud_plans
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_plans_update BEFORE UPDATE ON cloud_plans
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_plans_delete BEFORE DELETE ON cloud_plans
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_plan_revisions_insert BEFORE INSERT ON cloud_plan_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_plan_revisions_update BEFORE UPDATE ON cloud_plan_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_plan_revisions_delete BEFORE DELETE ON cloud_plan_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_theses_insert BEFORE INSERT ON cloud_theses
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_theses_update BEFORE UPDATE ON cloud_theses
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_theses_delete BEFORE DELETE ON cloud_theses
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_thesis_revisions_insert BEFORE INSERT ON cloud_thesis_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_thesis_revisions_update BEFORE UPDATE ON cloud_thesis_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_thesis_revisions_delete BEFORE DELETE ON cloud_thesis_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_transactions_insert BEFORE INSERT ON cloud_transactions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_transactions_update BEFORE UPDATE ON cloud_transactions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_transactions_delete BEFORE DELETE ON cloud_transactions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_manual_input_revisions_insert BEFORE INSERT ON cloud_manual_input_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_manual_input_revisions_update BEFORE UPDATE ON cloud_manual_input_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_manual_input_revisions_delete BEFORE DELETE ON cloud_manual_input_revisions
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_mutation_checks_insert BEFORE INSERT ON cloud_mutation_checks
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_mutation_checks_update BEFORE UPDATE ON cloud_mutation_checks
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_mutation_checks_delete BEFORE DELETE ON cloud_mutation_checks
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_outbox_insert BEFORE INSERT ON cloud_outbox
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND sends_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud send fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_update_claims_insert BEFORE INSERT ON cloud_update_claims
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND sends_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud send fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_report_claims_insert BEFORE INSERT ON cloud_report_claims
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND sends_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud send fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_outbox_begin_send BEFORE UPDATE OF status ON cloud_outbox
WHEN NEW.status='SENDING' AND EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND sends_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud send fence closed'); END;

-- Do not replay a legacy SENT, UNKNOWN, PENDING or FAILED report period.
CREATE TABLE IF NOT EXISTS cloud_legacy_report_holds (
    period_key TEXT PRIMARY KEY,
    report_type TEXT NOT NULL CHECK(report_type IN ('daily','weekly')),
    local_date TEXT NOT NULL,
    legacy_states TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS legacy_period_no_new_claim BEFORE INSERT ON cloud_report_claims
WHEN EXISTS (SELECT 1 FROM cloud_legacy_report_holds WHERE period_key=NEW.period_key)
BEGIN SELECT RAISE(ABORT,'legacy report period suppressed'); END;
CREATE TRIGGER IF NOT EXISTS legacy_period_immutable_update BEFORE UPDATE ON cloud_legacy_report_holds
BEGIN SELECT RAISE(ABORT,'legacy period immutable'); END;
CREATE TRIGGER IF NOT EXISTS legacy_period_immutable_delete BEFORE DELETE ON cloud_legacy_report_holds
BEGIN SELECT RAISE(ABORT,'legacy period immutable'); END;
CREATE TRIGGER IF NOT EXISTS legacy_period_sealed_insert BEFORE INSERT ON cloud_legacy_report_holds
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE state!='STAGING')
BEGIN SELECT RAISE(ABORT,'legacy period sealed'); END;

-- Ended native plans remain visible but cannot be implicitly resurrected.
CREATE TABLE IF NOT EXISTS cloud_plan_lifecycle (
    plan_key TEXT PRIMARY KEY REFERENCES cloud_plans(plan_key),
    ended_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS lifecycle_immutable_update BEFORE UPDATE ON cloud_plan_lifecycle
BEGIN SELECT RAISE(ABORT,'ended plan lifecycle immutable'); END;
CREATE TRIGGER IF NOT EXISTS lifecycle_immutable_delete BEFORE DELETE ON cloud_plan_lifecycle
BEGIN SELECT RAISE(ABORT,'ended plan lifecycle immutable'); END;
CREATE TRIGGER IF NOT EXISTS lifecycle_sealed_insert BEFORE INSERT ON cloud_plan_lifecycle
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE state!='STAGING')
BEGIN SELECT RAISE(ABORT,'ended plan lifecycle sealed'); END;
CREATE TRIGGER IF NOT EXISTS ended_plan_no_edit BEFORE UPDATE ON cloud_plans
WHEN EXISTS (SELECT 1 FROM cloud_plan_lifecycle WHERE plan_key=OLD.plan_key)
BEGIN SELECT RAISE(ABORT,'ended plan is read only'); END;
CREATE TRIGGER IF NOT EXISTS ended_plan_no_delete BEFORE DELETE ON cloud_plans
WHEN EXISTS (SELECT 1 FROM cloud_plan_lifecycle WHERE plan_key=OLD.plan_key)
BEGIN SELECT RAISE(ABORT,'ended plan is read only'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_snapshots_insert BEFORE INSERT ON cloud_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_snapshots_update BEFORE UPDATE ON cloud_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_snapshots_delete BEFORE DELETE ON cloud_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_snapshot_sources_insert BEFORE INSERT ON cloud_snapshot_sources
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_snapshot_sources_update BEFORE UPDATE ON cloud_snapshot_sources
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_snapshot_sources_delete BEFORE DELETE ON cloud_snapshot_sources
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_position_snapshots_insert BEFORE INSERT ON cloud_position_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_position_snapshots_update BEFORE UPDATE ON cloud_position_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_position_snapshots_delete BEFORE DELETE ON cloud_position_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_cash_snapshots_insert BEFORE INSERT ON cloud_cash_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_cash_snapshots_update BEFORE UPDATE ON cloud_cash_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

CREATE TRIGGER IF NOT EXISTS fence_cloud_cash_snapshots_delete BEFORE DELETE ON cloud_cash_snapshots
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='ACTIVE' AND edits_enabled=1))
BEGIN SELECT RAISE(ABORT,'cloud snapshot fence closed'); END;

-- Portfolio scope is an immutable identity, including native account_id=NULL plans.
CREATE TABLE IF NOT EXISTS cloud_plan_scope (
  plan_key TEXT PRIMARY KEY REFERENCES cloud_plans(plan_key),
  portfolio_id INTEGER NOT NULL CHECK (portfolio_id > 0)
);
CREATE TRIGGER IF NOT EXISTS plan_scope_insert BEFORE INSERT ON cloud_plan_scope
WHEN NOT EXISTS (SELECT 1 FROM cloud_cutover WHERE portfolio_id=NEW.portfolio_id AND
  (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'plan portfolio scope fence'); END;
CREATE TRIGGER IF NOT EXISTS plan_scope_update BEFORE UPDATE ON cloud_plan_scope
BEGIN SELECT RAISE(ABORT,'immutable plan portfolio scope'); END;
CREATE TRIGGER IF NOT EXISTS plan_scope_delete BEFORE DELETE ON cloud_plan_scope
BEGIN SELECT RAISE(ABORT,'immutable plan portfolio scope'); END;
CREATE TRIGGER IF NOT EXISTS new_plan_scope AFTER INSERT ON cloud_plans
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE state='ACTIVE' AND edits_enabled=1)
BEGIN INSERT INTO cloud_plan_scope SELECT NEW.plan_key,portfolio_id FROM cloud_cutover WHERE singleton=1; END;

-- Owner allocation policy (budget, caps, targets). Calculation input only;
-- it never creates an order. Revisions are append-only.
CREATE TABLE IF NOT EXISTS cloud_policy (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version >= 0),
    policy_json TEXT NOT NULL,
    last_mutation_key TEXT
);
CREATE TABLE IF NOT EXISTS cloud_policy_revisions (
    revision_no INTEGER PRIMARY KEY CHECK (revision_no > 0),
    policy_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    mutation_key TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS fence_cloud_policy_insert BEFORE INSERT ON cloud_policy
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;
CREATE TRIGGER IF NOT EXISTS fence_cloud_policy_update BEFORE UPDATE ON cloud_policy
WHEN EXISTS (SELECT 1 FROM cloud_cutover WHERE singleton=1 AND NOT (state='STAGING' OR (state='ACTIVE' AND edits_enabled=1)))
BEGIN SELECT RAISE(ABORT,'cloud edit fence closed'); END;
CREATE TRIGGER IF NOT EXISTS policy_revisions_immutable_update BEFORE UPDATE ON cloud_policy_revisions
BEGIN SELECT RAISE(ABORT,'policy revision immutable'); END;
CREATE TRIGGER IF NOT EXISTS policy_revisions_immutable_delete BEFORE DELETE ON cloud_policy_revisions
BEGIN SELECT RAISE(ABORT,'policy revision immutable'); END;
