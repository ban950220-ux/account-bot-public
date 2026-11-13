"""Produce only synthetic 29-table source/archive/core data for the Node tests."""
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from cloud_full_schema_fixture import FINGERPRINT, ROOT, seed

spec = importlib.util.spec_from_file_location("fixture_projection",ROOT/"scripts/cloud/migration_projection.py")
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)
root=Path(sys.argv[1])
seed(root/"source.db")
if "--ended" in sys.argv:
    with closing(sqlite3.connect(root/"source.db")) as db, db:
        db.execute("UPDATE recurring_buy_plans SET ended_at='2026-10-28T00:00:00Z',enabled=0")
        db.execute("UPDATE recurring_buy_revisions SET enabled=0")
if "--portfolio-null" in sys.argv:
    with closing(sqlite3.connect(root/"source.db")) as db, db:
        db.execute("UPDATE recurring_buy_plans SET account_id=NULL")
        db.execute("UPDATE recurring_buy_revisions SET account_id=NULL")
if "--report-case-matrix" in sys.argv:
    # Both native uppercase identities and the explicitly supported legacy forms.
    with closing(sqlite3.connect(root/"source.db")) as db, db:
        db.row_factory = sqlite3.Row
        report = dict(db.execute("SELECT * FROM reports WHERE id=1").fetchone())
        delivery = dict(db.execute("SELECT * FROM report_deliveries WHERE id=1").fetchone())
        db.execute("DELETE FROM report_deliveries")
        db.execute("DELETE FROM reports")
        for index, (kind, status) in enumerate(
            ((kind, status) for kind in ("DAILY", "WEEKLY", "daily", "weekly")
             for status in ("PENDING", "SENT", "FAILED", "UNKNOWN")), 1
        ):
            row = {**report, "id": index, "idempotency_key": f"report-matrix:{index}", "report_type": kind}
            db.execute(f"INSERT INTO reports ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
            row = {**delivery, "id": index, "report_id": index, "status": status}
            db.execute(f"INSERT INTO report_deliveries ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
audit=m.p.backup_and_audit(root/"source.db",root/"backup.db")
scope={"portfolioId":1,"ownerUserId":"42","accountFingerprints":[FINGERPRINT],"recurringAccountFingerprint":FINGERPRINT,"targetId":"synthetic-operating-target","manualKinds":[]}
if "--portfolio-null" in sys.argv:
    scope["recurringAccountFingerprint"]=None
manual=[]
if "--manual" in sys.argv or "--manual-missing-sections" in sys.argv:
    scope["manualKinds"]=["ACCOUNT_SNAPSHOT","RECURRING_PLANS"]
    instant="2026-10-29T00:00:00.000Z"
    docs=[{"kind":"ACCOUNT_SNAPSHOT","document":{"as_of":instant,"accounts":[{"account_id":"meritz-fixture","display_name":"synthetic","base_currency":"KRW"}],"positions":[],"cash":[],"transactions":[]}},
          {"kind":"RECURRING_PLANS","document":{"as_of":instant,"plans":[{"market":"US","ticker":"MTEST","asset_type":"ETF","instrument_currency":"USD","daily_amount":"1000","amount_currency":"KRW","broker":"meritz","started_at":None,"memo":"synthetic","enabled":True}]}}]
    if "--manual-missing-sections" in sys.argv:
        del docs[0]["document"]["cash"]
        del docs[0]["document"]["transactions"]
    manual=[{**doc,"dataAsOf":instant,"storedAt":instant} for doc in docs]
core=m.build_core_bundle(root/"backup.db",audit,scope=scope,manual_documents=manual)
(root/"core.json").write_text(m.encoded(core))
print(json.dumps({"archive":m.p.build_d1_archive_bundle(root/"backup.db",audit),"core":core}))
