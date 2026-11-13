"""Offline archive + scoped active projection. No CLI, credentials, or transport.

Every original table stays in the typed archive. Only explicitly scoped core
rows become active. REAL money requires an explicit digest-bound cell override;
we never infer decimal intent from binary storage or change the source database.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

_spec = importlib.util.spec_from_file_location("projection_preflight", Path(__file__).with_name("migration_preflight.py"))
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)
Blocked = p.MigrationBlocked

COLUMNS = {
    "cloud_accounts": "account_fingerprint broker masked_label base_currency".split(),
    "cloud_plans": "plan_key account_fingerprint source market ticker asset_type instrument_currency amount_currency daily_amount target_weight enabled timezone_name started_at memo version last_mutation_key".split(),
    "cloud_plan_revisions": "plan_key revision_no change_hash source account_fingerprint market ticker asset_type instrument_currency enabled daily_amount amount_currency target_weight timezone_name started_at effective_from memo mutation_key".split(),
    "cloud_transactions": "account_fingerprint source_key source market ticker asset_type currency transaction_type status traded_at settled_at quantity unit_price gross_amount fee tax net_amount".split(),
    "cloud_theses": "instrument_key market ticker currency current_revision_no last_mutation_key".split(),
    "cloud_thesis_revisions": "instrument_key revision_no revision_key content_hash thesis_text effective_at mutation_key".split(),
    "cloud_manual_input_revisions": "source kind revision mutation_key content_hash data_as_of stored_at document_json".split(),
    "cloud_legacy_report_holds": "period_key report_type local_date legacy_states".split(),
    "cloud_plan_scope": "plan_key portfolio_id".split(),
    "cloud_plan_lifecycle": "plan_key ended_at".split(),
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def scalar(value):
    if value is not None and not isinstance(value, (str, int)):
        raise Blocked("core projection requires text/integer/null")
    if isinstance(value, int) and (isinstance(value, bool) or abs(value) > 2**53 - 1):
        raise Blocked("unsafe integer")
    return value


def build_core_bundle(backup: Path, audit, *, scope: dict, decimal_overrides=None,
                      manual_documents=(), max_items=20) -> dict:
    """Return a private in-memory bundle; caller controls any future transfer.

    scope: {portfolioId, ownerUserId (decimal string), accountFingerprints,
            recurringAccountFingerprint, targetId}. No account number or key.
    Overrides: {table:id:column: {sourceSha, storageValue, decimal}}.
    """
    p.verify_backup_integrity(backup, audit)
    if (type(scope.get("portfolioId")) is not int or scope["portfolioId"] < 1 or
            not isinstance(scope.get("ownerUserId"), str) or
            not scope["ownerUserId"].isascii() or not scope["ownerUserId"].isdigit() or
            not 0 < int(scope["ownerUserId"]) <= 2**53 - 1 or
            not isinstance(scope.get("targetId"), str) or not 1 <= len(scope["targetId"]) <= 160 or
            not isinstance(scope.get("accountFingerprints"), list) or
            len(scope["accountFingerprints"]) != len(set(scope["accountFingerprints"])) or
            not scope["accountFingerprints"] or
            any(not p.FINGERPRINT.fullmatch(x) for x in scope["accountFingerprints"]) or
            ("recurringAccountFingerprint" not in scope or (scope["recurringAccountFingerprint"] is not None and scope["recurringAccountFingerprint"] not in scope["accountFingerprints"])) or
            type(max_items) is not int or not 1 <= max_items <= 20):
        raise Blocked("explicit valid portfolio/owner/account/target scope required")
    scope = dict(scope)
    overrides = decimal_overrides or {}
    used = set()
    def amount(row, table, field, optional=False):
        raw = row[field]
        if raw is None:
            if optional:
                return None
            raise Blocked(f"missing active amount:{table}:{field}")
        if isinstance(raw, float):
            key = f"{table}:{row['id']}:{field}"
            override = overrides.get(key)
            if (not math.isfinite(raw) or not isinstance(override, dict) or
                    override.get("sourceSha") != audit.logical_sha256 or
                    override.get("storageValue") != repr(raw)):
                raise Blocked(f"reviewed REAL decimal required:{table}:{field}")
            raw = override.get("decimal")
            used.add(key)
        try:
            return format(p._finite_amount(raw), "f")
        except (TypeError, ValueError) as error:
            raise Blocked("invalid active decimal") from error
    items = []
    def add(table, row):
        values = [scalar(row.get(key)) for key in COLUMNS[table]]
        item = {"table": table, "values": values}
        if len(encoded(item).encode()) > 16384:
            raise Blocked("core row exceeds bounded transfer size")
        items.append(item)
    uri = f"{Path(backup).resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise Blocked("source reference violation")
        portfolio = db.execute("SELECT * FROM portfolios WHERE id=?", (scope["portfolioId"],)).fetchone()
        if portfolio is None:
            raise Blocked("selected portfolio missing")
        if scope.get("reportTimezone",portfolio["timezone_name"]) != portfolio["timezone_name"]:
            raise Blocked("source portfolio timezone mismatch")
        scope["reportTimezone"] = portfolio["timezone_name"]
        accounts = {r["id"]: dict(r) for r in db.execute(
            "SELECT * FROM broker_accounts WHERE portfolio_id=?", (scope["portfolioId"],))}
        if set(r["account_fingerprint"] for r in accounts.values()) != set(scope["accountFingerprints"]):
            raise Blocked("account scope differs from frozen source")
        if len(accounts) != len(scope["accountFingerprints"]):
            raise Blocked("account fingerprint collision")
        for row in accounts.values():
            # Masked label stays in the exact archive, never guessed safe to publish.
            add("cloud_accounts", {**row, "masked_label": None})
        instruments = {r["id"]: dict(r) for r in db.execute("SELECT * FROM instruments")}
        def identity(row):
            item = instruments.get(row["instrument_id"])
            if item is None:
                raise Blocked("instrument reference missing")
            return dict(market=item["market"], ticker=item["ticker"], asset_type=item["asset_type"], instrument_currency=item["currency"])
        def fingerprint(row, optional=False):
            if optional and row["account_id"] is None:
                return None  # Native portfolio-scoped plan; never invent a broker account.
            account = accounts.get(row["account_id"])
            if account is None:
                raise Blocked("active plan/ledger has no explicit account mapping")
            return account["account_fingerprint"]
        mapping = []
        visible_plan_keys = set()
        for raw in db.execute("SELECT * FROM recurring_buy_plans WHERE portfolio_id=? ORDER BY id", (scope["portfolioId"],)):
            row = dict(raw)
            if row.get("ended_at") is not None and row["enabled"]:
                raise Blocked("ended plan cannot be active")
            base = {**row, **identity(row), "account_fingerprint": fingerprint(row, True),
                    "amount_currency": row["currency"], "daily_amount": amount(row, "recurring_buy_plans", "daily_amount"),
                    "target_weight": amount(row, "recurring_buy_plans", "target_weight", True),
                    "started_at": p._iso(row["started_at"]), "last_mutation_key": None}
            broker = "candidate" if "[source=USER_CANDIDATE]" in (row["memo"] or "") else "kiwoom_accum" if row["source"] == "TELEGRAM" else "meritz" if row["source"] == "MERITZ_SNAPSHOT" else row["source"].lower()
            visible_key = (base["market"],base["ticker"],base["asset_type"],base["instrument_currency"],broker)
            if visible_key in visible_plan_keys:
                raise Blocked("active plans would collide in report source scope")
            visible_plan_keys.add(visible_key)
            history = [dict(r) for r in db.execute("SELECT * FROM recurring_buy_revisions WHERE plan_id=? ORDER BY revision_no", (row["id"],))]
            if type(row["version"]) is not int or row["version"] < 1 or len(history) != row["version"]:
                raise Blocked("plan history count mismatch")
            projected = []
            for index, rev in enumerate(history, 1):
                if rev["revision_no"] != index:
                    raise Blocked("plan revision gap")
                projected.append({**rev, **identity(rev), "plan_key": row["plan_key"],
                    "account_fingerprint": fingerprint(rev, True), "amount_currency": rev["currency"],
                    "daily_amount": amount(rev, "recurring_buy_revisions", "daily_amount"),
                    "target_weight": amount(rev, "recurring_buy_revisions", "target_weight", True),
                    "started_at": p._iso(rev["started_at"]), "effective_from": p._iso(rev["effective_from"]), "mutation_key": None})
            for field in set(COLUMNS["cloud_plans"]) & set(COLUMNS["cloud_plan_revisions"]):
                left, right = base.get(field), projected[-1].get(field)
                if field in {"daily_amount", "target_weight"} and left is not None and right is not None:
                    left, right = p._finite_amount(left), p._finite_amount(right)
                if left != right:
                    raise Blocked(f"plan history head mismatch:{field}")
            add("cloud_plans", base)
            add("cloud_plan_scope", dict(plan_key=row["plan_key"], portfolio_id=row["portfolio_id"]))
            for rev in projected:
                add("cloud_plan_revisions", rev)
            if row.get("ended_at") is not None:
                add("cloud_plan_lifecycle",dict(plan_key=row["plan_key"],ended_at=p._iso(row["ended_at"])))
            mapping.append({"planKey": row["plan_key"], "sourceId": row["id"], "instrumentId": row["instrument_id"], "accountId": row["account_id"], "version": row["version"]})
        for raw in db.execute("SELECT * FROM transactions ORDER BY id"):
            row = dict(raw)
            if row["account_id"] not in accounts:
                continue
            inst = identity(row) if row["instrument_id"] is not None else dict(market=None, ticker=None, asset_type=None)
            add("cloud_transactions", {**row, **inst, "account_fingerprint": fingerprint(row),
                "source": accounts[row["account_id"]]["broker"], "traded_at": p._iso(row["traded_at"]),
                "settled_at": p._iso(row["settled_at"]) if row["settled_at"] else None,
                **{field: amount(row, "transactions", field, field != "quantity") for field in
                   ("quantity", "unit_price", "gross_amount", "fee", "tax", "net_amount")}})
        thesis_keys = set()
        for raw in db.execute("SELECT * FROM investment_theses WHERE portfolio_id=? ORDER BY id", (scope["portfolioId"],)):
            row = dict(raw)
            if row["status"] != "ACTIVE":
                continue  # Preserved in full archive, not resurrected.
            inst = identity(row)
            key = f"{inst['market']}:{inst['ticker']}"
            if key in thesis_keys:
                raise Blocked("ambiguous active thesis listing")
            thesis_keys.add(key)
            history = [dict(r) for r in db.execute("SELECT * FROM thesis_revisions WHERE thesis_id=? ORDER BY revision_no", (row["id"],))]
            if len(history) != row["current_revision_no"] or any(r["revision_no"] != n for n, r in enumerate(history, 1)):
                raise Blocked("thesis history mismatch")
            add("cloud_theses", dict(instrument_key=key, market=inst["market"], ticker=inst["ticker"], currency=inst["instrument_currency"], current_revision_no=row["current_revision_no"], last_mutation_key=None))
            for rev in history:
                add("cloud_thesis_revisions", {**rev, "instrument_key": key, "effective_at": p._iso(rev["effective_at"]), "mutation_key": f"legacy:{row['id']}:{rev['revision_no']}"})
        report_watermarks = [dict(r) for r in db.execute("""SELECT r.report_type,r.period_key,r.timezone_name,
            d.status FROM reports r JOIN report_deliveries d ON d.report_id=r.id
            WHERE r.portfolio_id=? ORDER BY r.id,d.id""", (scope["portfolioId"],))]
    # Native repository._identifier stores uppercase; older fixtures/imports may
    # contain lowercase. Map only these reviewed identities, never arbitrary casing.
    report_kinds = {"DAILY": "daily", "WEEKLY": "weekly", "daily": "daily", "weekly": "weekly"}
    holds = {}
    for report in report_watermarks:
        if report["report_type"] not in report_kinds or report["timezone_name"] != scope["reportTimezone"]:
            raise Blocked("legacy report type/timezone requires reviewed period mapping")
        try:
            local_date = p.datetime.fromisoformat(report["period_key"]).date().isoformat()
        except (TypeError,ValueError) as error:
            raise Blocked("legacy report period requires reviewed mapping") from error
        if local_date != report["period_key"] or report["status"] not in ("PENDING","SENT","FAILED","UNKNOWN"):
            raise Blocked("legacy report period/status invalid")
        key = report_kinds[report["report_type"]] + ":" + local_date.replace("-","")
        holds.setdefault(key,set()).add(report["status"])
    for key,states in sorted(holds.items()):
        kind,date = key.split(":")
        add("cloud_legacy_report_holds",dict(period_key=key,report_type=kind,local_date=f"{date[:4]}-{date[4:6]}-{date[6:]}",legacy_states=encoded(sorted(states))))
    if not isinstance(scope.get("manualKinds"), list) or sorted(scope["manualKinds"]) != sorted(doc["kind"] for doc in manual_documents):
        raise Blocked("explicit manual document inventory required")
    for doc in manual_documents:
        if doc["kind"] not in ("ACCOUNT_SNAPSHOT", "RECURRING_PLANS"):
            raise Blocked("unsupported manual document")
        add("cloud_manual_input_revisions", dict(source="meritz",kind=doc["kind"],revision=1,
            mutation_key="legacy:"+doc["kind"],content_hash=digest(doc["document"]),
            data_as_of=p._iso(doc["dataAsOf"]),stored_at=p._iso(doc["storedAt"]),document_json=encoded(doc["document"])))
    if used != set(overrides):
        raise Blocked("unused or wrong-scope decimal override")
    if len(items) > 5000 or len(encoded(items).encode()) > 1000000:
        raise Blocked("core projection exceeds reviewed offline batch envelope")
    counts = {table: sum(item["table"] == table for item in items) for table in COLUMNS}
    chunks = [{"index": index, "items": items[start:start + max_items]} for index, start in enumerate(range(0, len(items), max_items))]
    for chunk in chunks:
        chunk["sha"] = digest(chunk["items"])
    metadata = {"scope": scope, "planMapping": mapping, "reportWatermarks": report_watermarks,
                "decimalOverrides": overrides, "archiveOnly": "all original rows remain in typed archive"}
    manifest = {"kind": "CORE_PROJECTION_V1", "sourceLogicalSha": audit.logical_sha256,
        "backupSha": audit.backup_sha256, "archiveRunId": f"legacy:{audit.logical_sha256}",
        "scopeSha": digest(metadata), "rowsSha": digest(items), "counts": counts,
        "expectedChunks": len(chunks), "chunkRootSha": digest([c["sha"] for c in chunks])}
    manifest["runId"] = "core:" + digest(manifest)
    return {"manifest": manifest, "metadata": metadata, "chunks": chunks}
