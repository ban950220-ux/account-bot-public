from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/cloud/migration_preflight.py"
spec = importlib.util.spec_from_file_location("migration_preflight", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def synthetic_source(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE broker_accounts (id INTEGER PRIMARY KEY, broker TEXT,
                account_fingerprint TEXT, base_currency TEXT, masked_label TEXT);
            CREATE TABLE instruments (id INTEGER PRIMARY KEY, market TEXT, ticker TEXT,
                asset_type TEXT, currency TEXT);
            CREATE TABLE recurring_buy_plans (id INTEGER PRIMARY KEY, plan_key TEXT,
                instrument_id INTEGER, account_id INTEGER, version INTEGER,
                daily_amount TEXT, currency TEXT, enabled INTEGER, source TEXT,
                target_weight TEXT, timezone_name TEXT, started_at TEXT, memo TEXT);
            CREATE TABLE recurring_buy_revisions (plan_id INTEGER, revision_no INTEGER,
                change_hash TEXT, instrument_id INTEGER, account_id INTEGER,
                daily_amount TEXT, currency TEXT, enabled INTEGER, source TEXT,
                target_weight TEXT, timezone_name TEXT, started_at TEXT,
                effective_from TEXT, memo TEXT);
            CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER,
                source_key TEXT, instrument_id INTEGER, currency TEXT, quantity TEXT,
                status TEXT, transaction_type TEXT, traded_at TEXT, settled_at TEXT,
                unit_price TEXT, gross_amount TEXT, fee TEXT, tax TEXT, net_amount TEXT);
            CREATE TABLE report_deliveries (id INTEGER PRIMARY KEY, status TEXT);
        """)
        db.execute("INSERT INTO broker_accounts VALUES (1,'kiwoom',?,'KRW',NULL)",
                   ("fixture_" + "a" * 64,))
        db.execute("INSERT INTO instruments VALUES (1,'KOSPI','005930','EQUITY','KRW')")
        db.execute("""INSERT INTO recurring_buy_plans VALUES
            (1,'synthetic',1,1,1,'2000.25','KRW',1,'MANUAL',NULL,
             'Asia/Seoul','2026-01-01T00:00:00Z',NULL)""")
        db.execute("""INSERT INTO recurring_buy_revisions VALUES
            (1,1,?,1,1,'2000.25','KRW',1,'MANUAL',NULL,'Asia/Seoul',
             '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',NULL)""",
                   ("b" * 64,))
        db.execute("""INSERT INTO transactions VALUES
            (1,1,'trade-1',1,'KRW','1','FILLED','BUY','2026-01-01T00:00:00Z',
             NULL,'100',NULL,NULL,NULL,NULL)""")


def test_read_only_backup_and_audit_preserve_missing_and_original(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    before = source.read_bytes()
    audit = module.backup_and_audit(source, backup)
    assert source.read_bytes() == before
    assert audit.can_import
    assert audit.counts["transactions"] == 1
    assert audit.totals["plan_daily:KRW"] == "2000.25"
    assert audit.totals["transaction_gross:missing"] == "1"
    module.verify_backup(backup, audit)
    with pytest.raises(module.MigrationBlocked, match="distinct new backup"):
        module.backup_and_audit(source, backup)
    with closing(sqlite3.connect(backup)) as db, db:
        db.execute("INSERT INTO report_deliveries VALUES (1,'UNKNOWN')")
    with pytest.raises(module.MigrationBlocked, match="hash changed"):
        module.verify_backup(backup, audit)


def test_unmapped_delivery_and_revision_mismatch_block_import(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("INSERT INTO report_deliveries VALUES (1,'UNKNOWN')")
        db.execute("UPDATE recurring_buy_plans SET version=2")
    audit = module.backup_and_audit(source, backup)
    assert "unmapped_rows:report_deliveries" in audit.blockers
    assert "plan_revision_count_mismatch" in audit.blockers
    with pytest.raises(module.MigrationBlocked, match="unmapped or inconsistent"):
        module.verify_backup(backup, audit)


def test_duplicate_trade_key_is_explicit_conflict(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("""INSERT INTO transactions VALUES
            (2,1,'trade-1',1,'KRW','1','FILLED','BUY','2026-01-01T00:00:00Z',
             NULL,'100','10',NULL,NULL,NULL)""")
    audit = module.backup_and_audit(source, backup)
    assert "duplicate_transaction_source_key" in audit.blockers


def test_supported_synthetic_subset_import_is_atomic_and_reconciled(tmp_path: Path) -> None:
    source, backup, target = (tmp_path / name for name in
                              ("synthetic.db", "backup.db", "test-cloud.db"))
    synthetic_source(source)
    audit = module.backup_and_audit(source, backup)
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    counts = module.import_supported_test_subset(backup, audit, target, schema)
    assert counts == {"accounts": 1, "plans": 1, "plan_revisions": 1,
                      "transactions": 1}
    with closing(sqlite3.connect(target)) as db, db:
        assert db.execute("SELECT gross_amount FROM cloud_transactions").fetchone() == (None,)
        assert db.execute("SELECT daily_amount FROM cloud_plans").fetchone() == ("2000.25",)
        assert db.execute("SELECT status FROM cloud_transactions").fetchone() == ("FILLED",)
    with pytest.raises(module.MigrationBlocked, match="distinct new test target"):
        module.import_supported_test_subset(backup, audit, target, schema)


def test_orphan_revision_head_blocks_before_test_target_creation(tmp_path: Path) -> None:
    source, backup, target = (tmp_path / name for name in
                              ("synthetic.db", "backup.db", "test-cloud.db"))
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("UPDATE recurring_buy_revisions SET instrument_id = 999")
    audit = module.backup_and_audit(source, backup)
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    assert "plan_revision_head_mismatch" in audit.blockers
    with pytest.raises(module.MigrationBlocked, match="unmapped or inconsistent"):
        module.import_supported_test_subset(backup, audit, target, schema)
    assert not target.exists()


def test_destination_schema_must_match_before_target_creation(tmp_path: Path) -> None:
    source, backup, target = (tmp_path / name for name in
                              ("synthetic.db", "backup.db", "test-cloud.db"))
    synthetic_source(source)
    audit = module.backup_and_audit(source, backup)
    invalid_schema = tmp_path / "invalid.sql"
    invalid_schema.write_text("CREATE TABLE cloud_accounts (account_fingerprint TEXT);",
                              encoding="utf-8")
    with pytest.raises(module.MigrationBlocked, match="destination schema missing"):
        module.import_supported_test_subset(backup, audit, target, invalid_schema)
    assert not target.exists()


def test_nonempty_unmapped_column_blocks_import(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("ALTER TABLE broker_accounts ADD COLUMN portfolio_id INTEGER")
        db.execute("UPDATE broker_accounts SET portfolio_id = 1")
    audit = module.backup_and_audit(source, backup)
    assert "unmapped_column:broker_accounts" in audit.blockers


def test_missing_source_table_is_reported_without_unsafe_query(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("DROP TABLE transactions")
    audit = module.backup_and_audit(source, backup)
    assert "schema_missing:transactions" in audit.blockers


def test_orphan_ledger_is_preflight_blocker(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("UPDATE transactions SET account_id = 999")
    audit = module.backup_and_audit(source, backup)
    assert "orphan_transaction_account" in audit.blockers


def test_inert_archive_restores_legacy_events_without_active_replay(tmp_path: Path) -> None:
    source, backup, target, restored = (tmp_path / name for name in
        ("synthetic.db", "backup.db", "test-cloud.db", "restored.db"))
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("INSERT INTO report_deliveries VALUES (1,'UNKNOWN')")
        db.execute("""CREATE TABLE recurring_buy_occurrences
            (id INTEGER PRIMARY KEY, plan_id INTEGER, local_due_date TEXT, status TEXT)""")
        db.execute("INSERT INTO recurring_buy_occurrences VALUES (1,1,'2026-02-02','UNKNOWN')")
        db.execute("CREATE TABLE cash_snapshots (id INTEGER PRIMARY KEY, currency TEXT, amount TEXT)")
        db.execute("INSERT INTO cash_snapshots VALUES (1,'KRW','1000.125')")
        db.execute("INSERT INTO cash_snapshots VALUES (2,'USD','0.25')")
        db.execute("""CREATE TABLE position_snapshots
            (id INTEGER PRIMARY KEY, currency TEXT, market_value TEXT)""")
        db.execute("INSERT INTO position_snapshots VALUES (1,'USD',NULL)")
        db.execute("""CREATE TABLE legacy_extra (id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload BLOB, at TEXT, amount REAL)""")
        db.execute("INSERT INTO legacy_extra(payload,at,amount) VALUES (?,?,?)",
                   (b"\x00\xff", "2026-02-02T09:00:00+09:00", 0.125))
    audit = module.backup_and_audit(source, backup)
    assert not audit.can_import
    assert audit.totals["cash_snapshot:KRW"] == "1000.125"
    assert audit.totals["cash_snapshot:USD"] == "0.25"
    assert audit.totals["position_snapshot:missing"] == "1"
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    first = module.archive_legacy_test_db(backup, audit, target, schema)
    assert first["replay"] is False
    assert module.archive_legacy_test_db(backup, audit, target, schema)["replay"] is True
    with closing(sqlite3.connect(target)) as db, db:
        assert db.execute("SELECT count(*) FROM cloud_outbox").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM cloud_transactions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM cloud_report_claims").fetchone()[0] == 0
    module.restore_legacy_test_db(target, restored)
    with closing(sqlite3.connect(restored)) as db, db:
        assert db.execute("SELECT status FROM report_deliveries").fetchone() == ("UNKNOWN",)
        assert db.execute("SELECT status FROM recurring_buy_occurrences").fetchone() == ("UNKNOWN",)
        assert db.execute("SELECT payload,at,amount FROM legacy_extra").fetchone() == (
            b"\x00\xff", "2026-02-02T09:00:00+09:00", 0.125)
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='legacy_extra'").fetchone() == (1,)
    with closing(sqlite3.connect(source)) as before, closing(sqlite3.connect(restored)) as after:
        assert module._logical_digest(before) == module._logical_digest(after)


def test_archive_late_conflict_rolls_back_and_corruption_prevents_restore(tmp_path: Path) -> None:
    source, backup, target = (tmp_path / name for name in
        ("synthetic.db", "backup.db", "test-cloud.db"))
    synthetic_source(source)
    audit = module.backup_and_audit(source, backup)
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    with closing(sqlite3.connect(target)) as db, db:
        db.executescript(schema.read_text(encoding="utf-8"))
        db.execute("""CREATE TRIGGER fail_legacy AFTER INSERT ON cloud_legacy_archive_rows
            WHEN NEW.row_ordinal = 0 AND NEW.table_name = 'transactions'
            BEGIN SELECT RAISE(ABORT, 'synthetic late conflict'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic late conflict"):
        module.archive_legacy_test_db(backup, audit, target, schema)
    with closing(sqlite3.connect(target)) as db, db:
        assert db.execute("SELECT count(*) FROM cloud_legacy_archive_runs").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM cloud_legacy_archive_rows").fetchone() == (0,)
        db.execute("DROP TRIGGER fail_legacy")
    module.archive_legacy_test_db(backup, audit, target, schema)
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("""UPDATE cloud_legacy_archive_rows SET encoded_values = '[]'
            WHERE table_name = 'transactions'""")
    with pytest.raises(module.MigrationBlocked, match="archived row changed"):
        module.restore_legacy_test_db(target, tmp_path / "restored.db")
    assert not (tmp_path / "restored.db").exists()


def test_archive_refuses_existing_active_target(tmp_path: Path) -> None:
    source, backup, target = (tmp_path / name for name in
                              ("synthetic.db", "backup.db", "test-cloud.db"))
    synthetic_source(source)
    audit = module.backup_and_audit(source, backup)
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    with closing(sqlite3.connect(target)) as db, db:
        db.executescript(schema.read_text(encoding="utf-8"))
        db.execute("""INSERT INTO cloud_outbox
            (event_key,destination_key,payload_json,status,created_at)
            VALUES ('synthetic','test','{}','UNKNOWN','2026-02-02T00:00:00Z')""")
    with pytest.raises(module.MigrationBlocked, match="active operating target"):
        module.archive_legacy_test_db(backup, audit, target, schema)
    with closing(sqlite3.connect(target)) as db, db:
        assert db.execute("SELECT count(*) FROM cloud_legacy_archive_rows").fetchone() == (0,)


def test_archive_rejects_broken_source_reference(tmp_path: Path) -> None:
    source, backup, target = (tmp_path / name for name in
                              ("synthetic.db", "backup.db", "test-cloud.db"))
    synthetic_source(source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE referenced (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE broken (ref INTEGER REFERENCES referenced(id))")
        db.execute("INSERT INTO broken VALUES (99)")
    audit = module.backup_and_audit(source, backup)
    assert "source_foreign_key_violation" in audit.blockers
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    with pytest.raises(module.MigrationBlocked, match="source foreign key"):
        module.archive_legacy_test_db(backup, audit, target, schema)


def test_synthetic_backup_bundle_loads_through_d1_batch_api(tmp_path: Path) -> None:
    source, backup = tmp_path / "synthetic.db", tmp_path / "backup.db"
    synthetic_source(source)
    audit = module.backup_and_audit(source, backup)
    bundle = module.build_d1_archive_bundle(backup, audit, max_items=10)
    assert all(len(chunk["items"]) <= 10 for chunk in bundle["chunks"])
    bundle_path = tmp_path / "synthetic-only-bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    runner = Path(__file__).resolve().parents[1] / "tests/cloud_js_archive_runner.mjs"
    result = subprocess.run(["node", str(runner), str(bundle_path)],
                            capture_output=True, encoding="utf-8", timeout=30, check=True)
    assert json.loads(result.stdout) == {"status": "VERIFIED",
                                         "count": bundle["manifest"]["rowCount"]}


def test_archive_restore_closes_connections_before_windows_replace(tmp_path: Path, monkeypatch) -> None:
    source, backup, archive, restored = (tmp_path / name for name in
                                         ("source.db", "backup.db", "archive.db", "restored.db"))
    synthetic_source(source)
    audit = module.backup_and_audit(source, backup)
    schema = Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
    module.archive_legacy_test_db(backup, audit, archive, schema)
    opened = []
    real_connect, real_replace = sqlite3.connect, Path.replace

    class Tracked(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False
            opened.append(self)

        def close(self):
            super().close()
            self.closed = True

    def tracked_connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=Tracked)

    def windows_replace(path, target):
        if any(not db.closed for db in opened):
            raise PermissionError("Windows-style open SQLite handle blocks rename")
        return real_replace(path, target)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(Path, "replace", windows_replace)
    module.restore_legacy_test_db(archive, restored)
    assert restored.exists() and len(opened) == 2 and all(db.closed for db in opened)
    assert not restored.with_name(restored.name + ".partial").exists()
    partial = tmp_path / "existing.db.partial"
    partial.write_bytes(b"operator evidence; do not overwrite")
    with pytest.raises(module.MigrationBlocked, match="partial restore path already exists"):
        module.restore_legacy_test_db(archive, tmp_path / "existing.db")
    assert partial.read_bytes() == b"operator evidence; do not overwrite"
