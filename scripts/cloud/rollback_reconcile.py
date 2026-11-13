"""Offline reverse reconciliation into a NEW private directory, never live DB.

Only a frozen, paused cloud SQLite export is accepted. Exact cloud state and
manual documents travel with the restored DB. Startup is held until an operator
reconciles delivery uncertainty and proves the cloud/webhook have stopped.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import sys
from contextlib import ExitStack, closing
from decimal import Decimal, localcontext
from pathlib import Path

_spec = importlib.util.spec_from_file_location("rollback_projection", Path(__file__).with_name("migration_projection.py"))
m = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = m
_spec.loader.exec_module(m)
p = m.p
Blocked = m.Blocked


def _rows(db, table):
    return [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def _insert(db, table, values):
    columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
    values = {k: v for k, v in values.items() if k in columns}
    keys = list(values)
    db.execute(f'INSERT INTO "{table}" ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})', tuple(values[k] for k in keys))
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _native_decimal(value, field):
    """Match the declared native Numeric(28,8)/Numeric(18,10) boundary.

    SQLite readback alone is insufficient: the ORM also quantizes the scale.
    """
    if isinstance(value,float):
        if not math.isfinite(value):
            raise Blocked("nonfinite native amount")
        value = format(Decimal(str(value)),"f")
    amount = p._finite_amount(value)
    scale, precision = (10, 18) if field == "target_weight" else (8, 28)
    with localcontext() as context:
        context.prec = 128
        if amount != amount.quantize(Decimal(1).scaleb(-scale)) or abs(amount) >= Decimal(10) ** (precision-scale):
            raise Blocked("native Decimal field cannot preserve cloud precision")
    return amount


def _native_plan_hash(row, instrument_id, account_id):
    # Same native idempotency payload as repository.upsert_recurring_plan.
    def canonical(value):
        return "0" if value.is_zero() else format(value.normalize(), "f")
    weight = row["target_weight"]
    payload = dict(instrument_id=instrument_id,account_id=account_id,source=row["source"],
        enabled=bool(row["enabled"]),daily_amount=canonical(_native_decimal(row["daily_amount"],"daily_amount")),
        currency=row["amount_currency"],target_weight=None if weight is None else canonical(_native_decimal(weight,"target_weight")),
        timezone_name=row["timezone_name"],started_at=p.datetime.fromisoformat(p._iso(row["started_at"]).replace("Z","+00:00")).isoformat(),memo=row["memo"])
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def _restore_snapshots(out, state, accounts, portfolio_id, instrument):
    """Append scoped observations, preserving unavailable fields as NULL.

    No FX rate, price, profit or missing quantity is inferred. Existing native
    snapshot identities are never overwritten by a smaller cloud projection.
    """
    tables = ("cloud_snapshots", "cloud_snapshot_sources", "cloud_position_snapshots", "cloud_cash_snapshots")
    if sum(len(state.get(t, [])) for t in tables) > 5000:
        raise Blocked("snapshot reverse exceeds reviewed record envelope")
    snapshots = {r["idempotency_key"]: r for r in state.get("cloud_snapshots", [])}
    if any(r["snapshot_key"] not in snapshots for t in tables[1:] for r in state.get(t, [])):
        raise Blocked("orphan cloud snapshot child")

    def checked_insert(table, fields, decimals):
        record_id = _insert(out, table, fields)
        actual = out.execute(f'SELECT * FROM "{table}" WHERE id=?', (record_id,)).fetchone()
        for field in decimals:
            expected, stored = fields.get(field), actual[field]
            if (expected is None) != (stored is None) or (expected is not None and _native_decimal(expected,field) != _native_decimal(stored,field)):
                raise Blocked("native snapshot cannot preserve cloud decimal exactly")
        return record_id

    def account(value, optional=False):
        if value is None and optional:
            return None
        if value not in accounts:
            raise Blocked("snapshot account outside reverse scope")
        return accounts[value]

    for key, snap in snapshots.items():
        if out.execute("SELECT 1 FROM portfolio_snapshots WHERE idempotency_key=?", (key,)).fetchone():
            raise Blocked("cloud/native snapshot identity collision")
        sources = [r for r in state["cloud_snapshot_sources"] if r["snapshot_key"] == key]
        positions = [r for r in state["cloud_position_snapshots"] if r["snapshot_key"] == key]
        cash = [r for r in state["cloud_cash_snapshots"] if r["snapshot_key"] == key]
        if not sources or snap["status"] not in ("COLLECTING", "COMPLETE", "PARTIAL", "FAILED"):
            raise Blocked("snapshot coverage/state missing")
        if snap["status"] == "COMPLETE":
            if any(r["status"] != "SUCCESS" for r in sources) or snap["total_market_value"] is None or any(r["value_in_base"] is None for r in positions + cash):
                raise Blocked("complete snapshot has incomplete coverage")
            with localcontext() as context:
                context.prec = 256
                total = sum((p._finite_amount(r["value_in_base"]) for r in positions + cash), Decimal(0))
            if total != p._finite_amount(snap["total_market_value"]):
                raise Blocked("complete snapshot total mismatch")
        elif snap["total_market_value"] is not None or snap["total_cost_basis"] is not None:
            raise Blocked("partial snapshot cannot acquire a complete total")
        for field in ("captured_at", "data_as_of"):
            p._iso(snap[field])
        if snap["finalized_at"] is not None:
            p._iso(snap["finalized_at"])
        snapshot_id = checked_insert("portfolio_snapshots", {**snap, "portfolio_id": portfolio_id, "created_at": snap["captured_at"]}, ("total_market_value", "total_cost_basis"))
        for row in sources:
            _insert(out, "snapshot_sources", {**row, "snapshot_id": snapshot_id, "account_id": account(row["account_fingerprint"], True)})
        for row in positions:
            parts = row["instrument_key"].split(":")
            if len(parts) != 4 or parts[3] != row["currency"]:
                raise Blocked("snapshot listing identity ambiguous")
            item = dict(market=parts[0], ticker=parts[1], asset_type=parts[2], currency=parts[3])
            checked_insert("position_snapshots", {**row, "snapshot_id": snapshot_id,
                "account_id": account(row["account_fingerprint"]), "instrument_id": instrument(item)},
                ("quantity", "cost_basis", "market_value", "value_in_base"))
        for row in cash:
            checked_insert("cash_snapshots", {**row, "snapshot_id": snapshot_id,
                "account_id": account(row["account_fingerprint"])}, ("amount", "value_in_base"))
    return len(snapshots)


def reconcile_rollback(backup: Path, bundle: dict, cloud_snapshot: Path, destination: Path) -> dict:
    """Create a reviewable restored DB + exact journal, with startup held.

    Source backup and cloud snapshot must be immutable operator-created copies.
    No connection to remote D1, Telegram, the PC process or its live DB occurs.
    """
    backup, cloud_snapshot, destination = Path(backup), Path(cloud_snapshot), Path(destination)
    manifest, metadata = bundle["manifest"], bundle["metadata"]
    identity = {k: v for k, v in manifest.items() if k != "runId"}
    if (manifest["runId"] != "core:" + m.digest(identity) or
            m.digest(metadata) != manifest["scopeSha"] or
            p._file_digest(backup) != manifest["backupSha"] or destination.exists() or
            any(x.is_symlink() for x in [backup, cloud_snapshot, destination])):
        raise Blocked("rollback identity or new destination invalid")
    temporary = destination.with_name(destination.name + ".partial")
    if temporary.exists():
        raise Blocked("partial rollback already exists")
    if any(Path(str(file)+suffix).exists() for file in (backup,cloud_snapshot) for suffix in ("-wal","-journal")):
        raise Blocked("finalized standalone snapshots required; WAL/journal present")
    original_cloud_sha = p._file_digest(cloud_snapshot)
    def uri(path):
        return f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with ExitStack() as connections:
        source = connections.enter_context(closing(sqlite3.connect(uri(backup), uri=True)))
        cloud = connections.enter_context(closing(sqlite3.connect(uri(cloud_snapshot), uri=True)))
        cloud.row_factory = sqlite3.Row
        if p._logical_digest(source) != manifest["sourceLogicalSha"]:
            raise Blocked("frozen original differs")
        if cloud.execute("PRAGMA quick_check").fetchone()[0] != "ok" or cloud.execute("PRAGMA foreign_key_check").fetchone():
            raise Blocked("cloud snapshot integrity/reference failure")
        gate = cloud.execute("SELECT * FROM cloud_cutover WHERE singleton=1").fetchone()
        run = cloud.execute("SELECT * FROM cloud_core_import_runs WHERE run_id=?", (manifest["runId"],)).fetchone()
        if (gate is None or gate["state"] != "PAUSED" or gate["edits_enabled"] or gate["sends_enabled"] or
                gate["run_id"] != manifest["runId"] or run is None or run["status"] != "VERIFIED" or
                run["manifest_json"] != m.encoded(manifest) or run["metadata_json"] != m.encoded(metadata)):
            raise Blocked("matching paused cloud projection required")
        if cloud.execute("SELECT 1 FROM cloud_outbox WHERE status='SENDING' LIMIT 1").fetchone():
            raise Blocked("in-flight send requires explicit reconciliation before rollback snapshot")
        items = [item for chunk in bundle["chunks"] for item in chunk["items"]]
        if m.digest(items) != manifest["rowsSha"]:
            raise Blocked("original projection changed")
        baseline = {t: [dict(zip(m.COLUMNS[t], i["values"], strict=True)) for i in items if i["table"] == t] for t in m.COLUMNS}
        current = {t: _rows(cloud, t) for t in m.COLUMNS}
        if sorted(map(m.encoded, baseline["cloud_accounts"])) != sorted(map(m.encoded, current["cloud_accounts"])):
            raise Blocked("cloud account identity changed")
        if sorted(map(m.encoded,baseline["cloud_legacy_report_holds"])) != sorted(map(m.encoded,current["cloud_legacy_report_holds"])):
            raise Blocked("legacy report suppression changed")
        if sorted(map(m.encoded,baseline["cloud_plan_lifecycle"])) != sorted(map(m.encoded,current["cloud_plan_lifecycle"])):
            raise Blocked("archived plan lifecycle changed")
        scopes = {r["plan_key"]: r["portfolio_id"] for r in current["cloud_plan_scope"]}
        if (set(scopes) != {r["plan_key"] for r in current["cloud_plans"]} or
                any(v != metadata["scope"]["portfolioId"] for v in scopes.values()) or
                any(scopes.get(r["plan_key"]) != r["portfolio_id"] for r in baseline["cloud_plan_scope"])):
            raise Blocked("cloud plan portfolio scope changed")
        all_state = {t: _rows(cloud, t) for t in sorted(p._tables(cloud)) if t.startswith("cloud_") and not t.startswith("cloud_legacy_archive_")}
        for kind in ("ACCOUNT_SNAPSHOT", "RECURRING_PLANS"):
            docs = sorted((r for r in all_state.get("cloud_manual_input_revisions",[]) if r["kind"]==kind),key=lambda r:r["revision"])
            old_docs = [r for r in baseline["cloud_manual_input_revisions"] if r["kind"]==kind]
            if docs[:len(old_docs)] != old_docs or [r["revision"] for r in docs] != list(range(1,len(docs)+1)) or any(hashlib.sha256(r["document_json"].encode()).hexdigest()!=r["content_hash"] for r in docs):
                raise Blocked("manual document history diverged")
        temporary.mkdir(mode=0o700)
        try:
            output_path = temporary / "restored.sqlite3"
            with closing(sqlite3.connect(output_path)) as out, out:
                source.backup(out)
                out.row_factory = sqlite3.Row
                out.execute("PRAGMA foreign_keys=ON")
                out.execute("BEGIN IMMEDIATE")
                accounts = {r["account_fingerprint"]: r["id"] for r in _rows(out, "broker_accounts") if r["portfolio_id"] == metadata["scope"]["portfolioId"]}
                def plan_account(fp):
                    if fp is None:
                        return None
                    if fp not in accounts:
                        raise Blocked("reverse account unknown")
                    return accounts[fp]
                stamp = gate["activated_at"]
                def instrument(row):
                    currency = row.get("instrument_currency", row.get("currency"))
                    found = out.execute("SELECT id FROM instruments WHERE market=? AND ticker=? AND asset_type=? AND currency=?", (row["market"],row["ticker"],row["asset_type"],currency)).fetchall()
                    if len(found) > 1:
                        raise Blocked("ambiguous reverse instrument")
                    if found:
                        return found[0][0]
                    return _insert(out, "instruments", dict(market=row["market"],ticker=row["ticker"],asset_type=row["asset_type"],currency=currency,created_at=stamp,updated_at=stamp))
                old_plans = {r["plan_key"]: r for r in baseline["cloud_plans"]}
                new_plans = {r["plan_key"]: r for r in current["cloud_plans"]}
                if not old_plans.keys() <= new_plans.keys():
                    raise Blocked("cloud plan deletion cannot be silently restored")
                for key, row in new_plans.items():
                    history = sorted((r for r in current["cloud_plan_revisions"] if r["plan_key"] == key), key=lambda r:r["revision_no"])
                    old_history = sorted((r for r in baseline["cloud_plan_revisions"] if r["plan_key"] == key), key=lambda r:r["revision_no"])
                    if history[:len(old_history)] != old_history or len(history) != row["version"] or [r["revision_no"] for r in history] != list(range(1,row["version"]+1)):
                        raise Blocked("reverse plan revision diverged")
                    shared = set(m.COLUMNS["cloud_plans"]) & set(m.COLUMNS["cloud_plan_revisions"])
                    if any(row[k] != history[-1][k] for k in shared):
                        raise Blocked("reverse plan head mismatch")
                    if old_plans.get(key) == row:
                        continue  # Preserve unchanged native bytes and metadata.
                    native = out.execute("SELECT * FROM recurring_buy_plans WHERE plan_key=?", (key,)).fetchone()
                    if key not in old_plans and native is not None:
                        raise Blocked("new cloud plan collides with archived native plan")
                    if key in old_plans and (native is None or native["version"] != old_plans[key]["version"]):
                        raise Blocked("PC plan baseline diverged")
                    account_id = plan_account(row["account_fingerprint"])
                    fields = dict(instrument_id=instrument(row),account_id=account_id,source=row["source"],enabled=row["enabled"],daily_amount=row["daily_amount"],currency=row["amount_currency"],target_weight=row["target_weight"],timezone_name=row["timezone_name"],started_at=row["started_at"],memo=row["memo"],version=row["version"],updated_at=stamp)
                    if native is None:
                        plan_id = _insert(out,"recurring_buy_plans",{**fields,"plan_key":key,"portfolio_id":metadata["scope"]["portfolioId"],"created_at":stamp})
                    else:
                        plan_id = native["id"]
                        columns = list(fields)
                        out.execute(f'UPDATE recurring_buy_plans SET {",".join(k+"=?" for k in columns)} WHERE id=?',(*[fields[k] for k in columns],plan_id))
                    for rev in history[len(old_history):]:
                        revision_id = _insert(out,"recurring_buy_revisions",dict(plan_id=plan_id,revision_no=rev["revision_no"],change_hash=_native_plan_hash(rev,instrument(rev),plan_account(rev["account_fingerprint"])),instrument_id=instrument(rev),account_id=plan_account(rev["account_fingerprint"]),source=rev["source"],enabled=rev["enabled"],daily_amount=rev["daily_amount"],currency=rev["amount_currency"],target_weight=rev["target_weight"],timezone_name=rev["timezone_name"],started_at=rev["started_at"],effective_from=rev["effective_from"],memo=rev["memo"],created_at=stamp))
                        stored_revision = out.execute("SELECT daily_amount,target_weight FROM recurring_buy_revisions WHERE id=?",(revision_id,)).fetchone()
                        for field,value in zip(("daily_amount","target_weight"),stored_revision,strict=True):
                            expected=rev[field]
                            if (expected is None)!=(value is None) or (expected is not None and _native_decimal(value,field)!=_native_decimal(expected,field)):
                                raise Blocked("native revision cannot preserve cloud decimal exactly")
                    # SQLite NUMERIC affinity may round large/fractional values. Refuse
                    # any changed decimal after storage; exact cloud strings stay journaled.
                    got = out.execute("SELECT daily_amount,target_weight FROM recurring_buy_plans WHERE id=?",(plan_id,)).fetchone()
                    for field,value in zip(("daily_amount","target_weight"),got,strict=True):
                        expected = row[field]
                        if (expected is None) != (value is None) or (expected is not None and _native_decimal(value,field) != _native_decimal(expected,field)):
                            raise Blocked("native SQLite cannot preserve cloud decimal exactly")
                # Existing ledger records are immutable; append only unseen source keys.
                def ledger_key(row):
                    return row["account_fingerprint"], row["source_key"]
                old_ledger = {ledger_key(r):r for r in baseline["cloud_transactions"]}
                active_ledger = {ledger_key(r):r for r in current["cloud_transactions"]}
                if not old_ledger.keys() <= active_ledger.keys() or any(active_ledger[k] != v for k,v in old_ledger.items()):
                    raise Blocked("cloud baseline ledger altered")
                for key,row in active_ledger.items():
                    if key in old_ledger:
                        continue
                    if row["account_fingerprint"] not in accounts:
                        raise Blocked("new ledger scope unknown")
                    transaction_id = _insert(out,"transactions",{**row,"account_id":accounts[row["account_fingerprint"]],"instrument_id":instrument(row) if row["ticker"] is not None else None,"created_at":stamp})
                    stored = out.execute("SELECT * FROM transactions WHERE id=?",(transaction_id,)).fetchone()
                    for field in ("quantity","unit_price","gross_amount","fee","tax","net_amount"):
                        expected,value=row[field],stored[field]
                        if (expected is None)!=(value is None) or (expected is not None and _native_decimal(value,field)!=_native_decimal(expected,field)):
                            raise Blocked("native ledger cannot preserve cloud decimal exactly")
                # Current thesis and its append-only history map back to the original
                # instrument. A new ambiguous market/ticker listing is refused.
                old_theses = {r["instrument_key"]:r for r in baseline["cloud_theses"]}
                new_theses = {r["instrument_key"]:r for r in current["cloud_theses"]}
                if not old_theses.keys() <= new_theses.keys():
                    raise Blocked("thesis deletion cannot be silently restored")
                for key,row in new_theses.items():
                    history = sorted((r for r in current["cloud_thesis_revisions"] if r["instrument_key"]==key),key=lambda r:r["revision_no"])
                    old_history = sorted((r for r in baseline["cloud_thesis_revisions"] if r["instrument_key"]==key),key=lambda r:r["revision_no"])
                    if history[:len(old_history)] != old_history or [r["revision_no"] for r in history] != list(range(1,row["current_revision_no"]+1)):
                        raise Blocked("thesis history diverged")
                    found = out.execute("SELECT id FROM instruments WHERE market=? AND ticker=? AND currency=?",(row["market"],row["ticker"],row["currency"])).fetchall()
                    if len(found)!=1:
                        raise Blocked("thesis instrument requires exact listing mapping")
                    inst_id=found[0][0]
                    native=out.execute("SELECT id FROM investment_theses WHERE portfolio_id=? AND instrument_id=?",(metadata["scope"]["portfolioId"],inst_id)).fetchone()
                    thesis_id=native[0] if native else _insert(out,"investment_theses",dict(portfolio_id=metadata["scope"]["portfolioId"],instrument_id=inst_id,status="ACTIVE",current_revision_no=0,created_at=stamp,updated_at=stamp))
                    for rev in history[len(old_history):]:
                        _insert(out,"thesis_revisions",{**rev,"thesis_id":thesis_id,"created_at":stamp})
                    out.execute("UPDATE investment_theses SET current_revision_no=?,updated_at=? WHERE id=?",(row["current_revision_no"],stamp,thesis_id))
                restored_snapshots = _restore_snapshots(out, all_state, accounts, metadata["scope"]["portfolioId"], instrument)
                if out.execute("PRAGMA foreign_key_check").fetchone():
                    raise Blocked("reverse foreign key failure")
                out.execute("CREATE TABLE cloud_rollback_gate (singleton INTEGER PRIMARY KEY, state TEXT NOT NULL, cloud_sha TEXT NOT NULL, resume_after TEXT, proof_json TEXT)")
                out.execute("INSERT INTO cloud_rollback_gate VALUES (1,'HELD',?,NULL,NULL)",(original_cloud_sha,))
                out.commit()
            os.chmod(output_path,0o600)
            journal=m.encoded(all_state)
            (temporary/"cloud-state.json").write_text(journal,encoding="utf-8")
            for kind,name in (("ACCOUNT_SNAPSHOT","meritz-account.json"),("RECURRING_PLANS","meritz-recurring.json")):
                rows=[r for r in all_state.get("cloud_manual_input_revisions",[]) if r["kind"]==kind]
                if rows:
                    latest=max(rows,key=lambda r:r["revision"])
                    if hashlib.sha256(latest["document_json"].encode()).hexdigest()!=latest["content_hash"]:
                        raise Blocked("manual revision content changed")
                    (temporary/name).write_text(latest["document_json"],encoding="utf-8")
            result={"kind":"ROLLBACK_RECONCILIATION_V1","sourceSha":manifest["sourceLogicalSha"],"cloudSha":original_cloud_sha,"runId":manifest["runId"],"startup":"HELD","restoredSnapshots":restored_snapshots,"newPlanRevisions":len(current["cloud_plan_revisions"])-len(baseline["cloud_plan_revisions"]),"uncertainDeliveries":sum(r["status"] in ("UNKNOWN","PENDING") for r in all_state["cloud_outbox"]),"files":{f.name:p._file_digest(f) for f in temporary.iterdir()}}
            (temporary/"reconciliation.json").write_text(m.encoded(result),encoding="utf-8")
            for f in temporary.iterdir():
                os.chmod(f,0o600)
            if p._file_digest(cloud_snapshot)!=original_cloud_sha or p._file_digest(backup)!=manifest["backupSha"]:
                raise Blocked("input changed during reverse reconciliation")
            # SQLite transaction context managers do not close connections. Release
            # every handle before atomic publication (required by Windows locking).
            connections.close()
            if destination.exists():
                raise Blocked("rollback destination appeared during reconciliation")
            temporary.rename(destination)
            return result
        except BaseException:
            shutil.rmtree(temporary)
            raise
