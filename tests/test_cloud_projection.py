"""Full schema synthetic tests. No dependency installation or operating data."""
import importlib.util
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from cloud_full_schema_fixture import FINGERPRINT, ROOT, seed

spec=importlib.util.spec_from_file_location("unit_projection",ROOT/"scripts/cloud/migration_projection.py")
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        seed(self.root/"source.db")
        self.scope={"portfolioId":1,"ownerUserId":"42","accountFingerprints":[FINGERPRINT],"recurringAccountFingerprint":FINGERPRINT,"targetId":"fixture","manualKinds":[]}

    def bundle(self, **options):
        backup=self.root/"backup.db"
        backup.unlink(missing_ok=True)
        audit=m.p.backup_and_audit(self.root/"source.db",backup)
        return m.build_core_bundle(backup,audit,scope=self.scope,**options),audit

    def test_all_tables_preserved_but_only_scoped_core_is_active(self):
        original=m.p._file_digest(self.root/"source.db")
        bundle,audit=self.bundle()
        self.assertFalse(audit.can_import)  # Legacy subset audit remains honest.
        self.assertEqual(bundle["manifest"]["counts"]["cloud_plans"],1)
        self.assertEqual(bundle["metadata"]["reportWatermarks"][0]["status"],"UNKNOWN")
        archive=m.p.build_d1_archive_bundle(self.root/"backup.db",audit)
        self.assertEqual(archive["manifest"]["tableCount"],29)
        m.p.archive_legacy_test_db(self.root/"backup.db",audit,self.root/"archive.db",ROOT/"deploy/cloudflare/schema.sql")
        m.p.restore_legacy_test_db(self.root/"archive.db",self.root/"restored.db")
        with closing(sqlite3.connect(self.root/"restored.db")) as db, db:
            self.assertEqual(m.p._logical_digest(db),audit.logical_sha256)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='index'").fetchone()[0],65)
        self.assertEqual(m.p._file_digest(self.root/"source.db"),original)

    def test_active_real_requires_source_bound_explicit_override(self):
        with closing(sqlite3.connect(self.root/"source.db")) as db, db:
            db.execute("UPDATE recurring_buy_plans SET daily_amount=2000.25")
            db.execute("UPDATE recurring_buy_revisions SET daily_amount=2000.25")
        with self.assertRaisesRegex(m.Blocked,"REAL decimal"):
            self.bundle()
        audit=m.p.backup_and_audit(self.root/"source.db",self.root/"override-backup.db")
        overrides={f"{table}:1:daily_amount":{"sourceSha":audit.logical_sha256,"storageValue":"2000.25","decimal":"2000.25000000"} for table in ("recurring_buy_plans","recurring_buy_revisions")}
        bundle=m.build_core_bundle(self.root/"override-backup.db",audit,scope=self.scope,decimal_overrides=overrides)
        self.assertEqual(bundle["metadata"]["decimalOverrides"],overrides)
        overrides["recurring_buy_plans:1:daily_amount"]["sourceSha"]="0"*64
        with self.assertRaises(m.Blocked):
            m.build_core_bundle(self.root/"override-backup.db",audit,scope=self.scope,decimal_overrides=overrides)

    def test_missing_account_or_wrong_owner_scope_cannot_be_guessed(self):
        self.scope["accountFingerprints"].append("other_"+"b"*64)
        with self.assertRaisesRegex(m.Blocked,"account scope"):
            self.bundle()
        self.scope["accountFingerprints"]=[FINGERPRINT]
        self.scope["ownerUserId"]="0"
        with self.assertRaises(m.Blocked):
            self.bundle()

    def test_nonfinite_and_head_mismatch_remain_blocked(self):
        for value in ("NaN","Infinity","1e999999"):
            with self.subTest(value=value):
                with closing(sqlite3.connect(self.root/"source.db")) as db, db:
                    db.execute("UPDATE recurring_buy_plans SET daily_amount=?",(value,))
                with self.assertRaises(m.Blocked):
                    self.bundle()
        with closing(sqlite3.connect(self.root/"source.db")) as db, db:
            db.execute("UPDATE recurring_buy_plans SET daily_amount=3000")
        with self.assertRaisesRegex(m.Blocked,"head mismatch"):
            self.bundle()

    def test_external_manual_inventory_cannot_be_silently_omitted(self):
        self.scope["manualKinds"]=["RECURRING_PLANS"]
        with self.assertRaisesRegex(m.Blocked,"manual document inventory"):
            self.bundle()

    def test_scoped_metadata_does_not_drop_ended_or_unassigned_active_plan(self):
        with closing(sqlite3.connect(self.root/"source.db")) as db, db:
            db.execute("UPDATE recurring_buy_plans SET ended_at='2026-10-01T00:00:00Z'")
        with self.assertRaisesRegex(m.Blocked,"ended plan"):
            self.bundle()
        with closing(sqlite3.connect(self.root/"source.db")) as db, db:
            db.execute("UPDATE recurring_buy_plans SET ended_at=NULL,account_id=NULL")
            db.execute("UPDATE recurring_buy_revisions SET account_id=NULL")
        self.scope["recurringAccountFingerprint"]=None
        bundle,_=self.bundle()
        rows=[r for c in bundle["chunks"] for r in c["items"]]
        self.assertTrue(all(r["values"][1] is None for r in rows if r["table"]=="cloud_plans"))
        self.assertEqual([r["values"] for r in rows if r["table"]=="cloud_plan_scope"],[["fixture:US:TEST",1]])


class NativeReverseBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import runpy
        cls.reverse = runpy.run_path(str(ROOT/"scripts/cloud/rollback_reconcile.py"))

    def test_native_scale_is_checked_even_when_sqlite_float_repr_looks_equal(self):
        from decimal import Decimal
        check = self.reverse["_native_decimal"]
        self.assertEqual(check(1e-8,"daily_amount"),Decimal("0.00000001"))
        with self.assertRaisesRegex(self.reverse["Blocked"],"precision"):
            check("0.12345678901","target_weight")
        with self.assertRaisesRegex(self.reverse["Blocked"],"precision"):
            check("1000.000000001","daily_amount")

    def test_native_revision_hash_uses_native_ids_aware_time_and_decimal_payload(self):
        import hashlib
        import json
        row=dict(source="TELEGRAM",enabled=0,daily_amount="1500.0000",amount_currency="KRW",target_weight=None,timezone_name="Asia/Seoul",started_at="2026-10-29T00:00:00Z",memo="synthetic")
        payload=dict(instrument_id=1,account_id=1,source="TELEGRAM",enabled=False,daily_amount="1500",currency="KRW",target_weight=None,timezone_name="Asia/Seoul",started_at="2026-10-29T00:00:00+00:00",memo="synthetic")
        expected=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        self.assertEqual(self.reverse["_native_plan_hash"](row,1,1),expected)


if __name__=="__main__":
    unittest.main()
