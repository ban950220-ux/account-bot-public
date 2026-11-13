"""Offline backup and loss audit for a future, separately approved migration.

No CLI is provided: this module must not be pointed at an operating database by
the cloud setup/startup hooks. Tests use disposable synthetic SQLite databases.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from base64 import b64decode, b64encode
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

SOURCE_COLUMNS = {
    "broker_accounts": {"id", "broker", "account_fingerprint", "base_currency",
                        "masked_label"},
    "instruments": {"id", "market", "ticker", "asset_type", "currency"},
    "recurring_buy_plans": {"id", "plan_key", "instrument_id", "account_id", "version",
                            "daily_amount", "currency", "enabled", "source",
                            "target_weight", "timezone_name", "started_at", "memo"},
    "recurring_buy_revisions": {"plan_id", "revision_no", "change_hash", "instrument_id",
                                "account_id", "daily_amount", "currency", "enabled",
                                "source", "target_weight", "timezone_name", "started_at",
                                "effective_from", "memo"},
    "transactions": {"account_id", "source_key", "instrument_id", "currency",
                     "quantity", "status", "transaction_type", "traded_at",
                     "settled_at", "unit_price", "gross_amount", "fee", "tax",
                     "net_amount"},
}
MAPPED_COLUMNS = {
    "broker_accounts": SOURCE_COLUMNS["broker_accounts"],
    "instruments": SOURCE_COLUMNS["instruments"],
    "recurring_buy_plans": SOURCE_COLUMNS["recurring_buy_plans"],
    "recurring_buy_revisions": SOURCE_COLUMNS["recurring_buy_revisions"],
    "transactions": SOURCE_COLUMNS["transactions"] | {"id"},
}

FINGERPRINT = re.compile(r"[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}\Z")
TARGET_COLUMNS = {
    "cloud_accounts": {"account_fingerprint", "broker", "masked_label", "base_currency"},
    "cloud_plans": {"plan_key", "account_fingerprint", "daily_amount", "version"},
    "cloud_plan_revisions": {"plan_key", "revision_no", "change_hash"},
    "cloud_transactions": {"account_fingerprint", "source_key", "gross_amount", "status"},
}
ROLLBACK_STATE_TABLES = (
    "cloud_accounts", "cloud_plans", "cloud_plan_revisions", "cloud_transactions",
    "cloud_snapshots", "cloud_snapshot_sources", "cloud_position_snapshots",
    "cloud_cash_snapshots", "cloud_theses", "cloud_thesis_revisions",
    "cloud_manual_input_revisions", "cloud_mutation_checks", "cloud_update_claims",
    "cloud_report_claims", "cloud_outbox",
)


class MigrationBlocked(RuntimeError):
    """The source cannot be copied losslessly with the current destination schema."""


@dataclass(frozen=True)
class Audit:
    backup_sha256: str
    logical_sha256: str
    counts: dict[str, int]
    totals: dict[str, str]
    blockers: tuple[str, ...]

    @property
    def can_import(self) -> bool:
        return not self.blockers


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _archive_tables(db: sqlite3.Connection) -> set[str]:
    names = _tables(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        names.add("sqlite_sequence")
    return names


def _logical_digest(db: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for name in sorted(_archive_tables(db)):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise MigrationBlocked("unsafe source table name")
        digest.update(name.encode())
        for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            digest.update(json.dumps(tuple(row), ensure_ascii=False,
                                     default=str, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _amount_totals(db: sqlite3.Connection, table: str, column: str,
                   category: str, currency_column: str = "currency") -> dict[str, str]:
    sums: dict[str, Decimal] = {}
    missing = 0
    for currency, raw in db.execute(
        f'SELECT "{currency_column}", "{column}" FROM "{table}"'
    ):
        if raw is None:
            missing += 1
            continue
        try:
            amount = _finite_amount(raw)
            previous = sums.get(currency, Decimal(0))
            # Reconciliation must not round at Decimal's default 28 digits.
            left, right = previous.as_tuple(), amount.as_tuple()
            with localcontext() as context:
                context.prec = max(28, len(left.digits) + len(right.digits) +
                                   abs(left.exponent - right.exponent) + 1)
                sums[currency] = previous + amount
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MigrationBlocked(f"invalid amount:{table}") from exc
    return {**{f"{category}:{currency}": format(value, "f")
               for currency, value in sums.items()}, f"{category}:missing": str(missing)}


def _finite_amount(raw: object) -> Decimal:
    text = str(raw)
    if len(text) > 100 or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text, flags=re.ASCII):
        raise ValueError("finite fixed-point decimal required")
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError("finite fixed-point decimal required")
    return value


def _plan_history_blockers(db: sqlite3.Connection) -> list[str]:
    blockers = []
    fields = sorted(SOURCE_COLUMNS["recurring_buy_plans"] &
                    SOURCE_COLUMNS["recurring_buy_revisions"])
    selected = ",".join(f'"{field}"' for field in fields)
    for row in db.execute(f'SELECT id, version, {selected} FROM recurring_buy_plans'):
        plan_id, version, *current = row
        history = db.execute(f'SELECT revision_no, {selected} FROM recurring_buy_revisions '
                             'WHERE plan_id = ? ORDER BY revision_no', (plan_id,)).fetchall()
        if (not isinstance(version, int) or version < 1 or len(history) != version or
                any(item[0] != index for index, item in enumerate(history, start=1))):
            blockers.append("plan_revision_order_mismatch")
            continue
        for field, actual, recorded in zip(fields, current, history[-1][1:], strict=True):
            try:
                if field in {"daily_amount", "target_weight"}:
                    actual = None if actual is None else _finite_amount(actual)
                    recorded = None if recorded is None else _finite_amount(recorded)
                elif field == "started_at":
                    actual, recorded = _iso(actual), _iso(recorded)
            except (InvalidOperation, TypeError, ValueError, MigrationBlocked):
                blockers.append("plan_revision_head_mismatch")
                break
            if actual != recorded:
                blockers.append("plan_revision_head_mismatch")
                break
    return blockers


def backup_and_audit(source: Path, backup: Path) -> Audit:
    """Make an online SQLite backup; verify logical equality; report safe counts only."""
    source = Path(source).resolve(strict=True)
    backup = Path(backup).resolve()
    if source == backup or backup.exists() or not source.is_file():
        raise MigrationBlocked("distinct new backup path required")
    uri = f"{source.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as original, original:
        if original.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationBlocked("source integrity check failed")
        with closing(sqlite3.connect(backup)) as copy, copy:
            original.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationBlocked("backup integrity check failed")
            source_digest = _logical_digest(original)
            copied_digest = _logical_digest(copy)
            if source_digest != copied_digest:
                raise MigrationBlocked("source changed or backup differs")
            tables = _tables(copy)
            blockers = []
            if copy.execute("PRAGMA foreign_key_check").fetchone():
                blockers.append("source_foreign_key_violation")
            compatible = set()
            for table, required in SOURCE_COLUMNS.items():
                if table not in tables:
                    blockers.append(f"schema_missing:{table}")
                    continue
                columns = {row[1] for row in copy.execute(f'PRAGMA table_info("{table}")')}
                if not required <= columns:
                    blockers.append(f"columns_missing:{table}")
                else:
                    compatible.add(table)
                for column in columns - MAPPED_COLUMNS[table]:
                    if not re.fullmatch(r"[a-z][a-z0-9_]*", column) or copy.execute(
                        f'SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1'
                    ).fetchone():
                        blockers.append(f"unmapped_column:{table}")
            counts = {table: copy.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                      for table in sorted(tables) if re.fullmatch(r"[a-z][a-z0-9_]*", table)}
            totals: dict[str, str] = {}
            for table, amount_column, category in (
                ("recurring_buy_plans", "daily_amount", "plan_daily"),
                ("transactions", "gross_amount", "transaction_gross"),
            ):
                if table not in compatible:
                    continue
                columns = {row[1] for row in copy.execute(f'PRAGMA table_info("{table}")')}
                if not {"currency", amount_column} <= columns:
                    blockers.append(f"total_columns_missing:{table}")
                    continue
                try:
                    totals.update(_amount_totals(copy, table, amount_column, category))
                except MigrationBlocked:
                    blockers.append(f"invalid_amount:{table}")
            for table, amount_column, category, currency_column in (
                ("cash_snapshots", "amount", "cash_snapshot", "currency"),
                ("position_snapshots", "market_value", "position_snapshot", "currency"),
                ("portfolio_snapshots", "total_market_value", "portfolio_snapshot",
                 "base_currency"),
            ):
                if not counts.get(table):
                    continue
                columns = {row[1] for row in copy.execute(f'PRAGMA table_info("{table}")')}
                if not {currency_column, amount_column} <= columns:
                    blockers.append(f"total_columns_missing:{table}")
                    continue
                try:
                    totals.update(_amount_totals(copy, table, amount_column, category,
                                                 currency_column))
                except MigrationBlocked:
                    blockers.append(f"invalid_amount:{table}")
            for table in sorted(tables - SOURCE_COLUMNS.keys()):
                if counts[table]:
                    blockers.append(f"unmapped_rows:{table}")
            if "broker_accounts" in compatible:
                if copy.execute("""SELECT 1 FROM broker_accounts
                    GROUP BY account_fingerprint HAVING count(*) > 1 LIMIT 1""").fetchone():
                    blockers.append("account_fingerprint_collision")
                for fingerprint, in copy.execute(
                    "SELECT account_fingerprint FROM broker_accounts"
                ):
                    if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
                        blockers.append("invalid_account_fingerprint")
                        break
            if {"recurring_buy_plans", "recurring_buy_revisions"} <= compatible:
                blockers.extend(_plan_history_blockers(copy))
                if copy.execute("""SELECT 1 FROM recurring_buy_plans AS p
                    LEFT JOIN (SELECT plan_id, count(*) AS n FROM recurring_buy_revisions
                      GROUP BY plan_id) AS r ON r.plan_id = p.id
                    WHERE p.version <> COALESCE(r.n, 0) LIMIT 1""").fetchone():
                    blockers.append("plan_revision_count_mismatch")
                if copy.execute("""SELECT 1 FROM recurring_buy_revisions AS r
                    LEFT JOIN recurring_buy_plans AS p ON p.id = r.plan_id
                    WHERE p.id IS NULL LIMIT 1""").fetchone():
                    blockers.append("orphan_plan_revision")
            if "transactions" in compatible and copy.execute("""SELECT 1 FROM transactions
                GROUP BY account_id, source_key HAVING count(*) > 1 LIMIT 1""").fetchone():
                blockers.append("duplicate_transaction_source_key")
            if {"transactions", "broker_accounts"} <= compatible and copy.execute("""
                SELECT 1 FROM transactions AS t LEFT JOIN broker_accounts AS a
                  ON a.id = t.account_id WHERE a.id IS NULL LIMIT 1
            """).fetchone():
                blockers.append("orphan_transaction_account")
            for table, column in (("recurring_buy_plans", "daily_amount"),
                                  ("recurring_buy_plans", "target_weight"),
                                  ("recurring_buy_revisions", "daily_amount"),
                                  ("recurring_buy_revisions", "target_weight"),
                                  ("transactions", "quantity"),
                                  ("transactions", "unit_price"),
                                  ("transactions", "gross_amount"),
                                  ("transactions", "fee"),
                                  ("transactions", "tax"),
                                  ("transactions", "net_amount")):
                if table in compatible:
                    if column in {"daily_amount", "quantity"} and copy.execute(
                        f'SELECT 1 FROM "{table}" WHERE "{column}" IS NULL LIMIT 1'
                    ).fetchone():
                        blockers.append(f"missing_amount:{table}:{column}")
                    for raw, in copy.execute(f'SELECT "{column}" FROM "{table}" '
                                             f'WHERE "{column}" IS NOT NULL'):
                        try:
                            _finite_amount(raw)
                        except (InvalidOperation, TypeError, ValueError):
                            blockers.append(f"invalid_amount:{table}:{column}")
                            break
                if table in compatible and copy.execute(
                    f'SELECT 1 FROM "{table}" WHERE typeof("{column}") = \'real\' LIMIT 1'
                ).fetchone():
                    blockers.append(f"imprecise_sqlite_real:{table}")
    checksum = _file_digest(backup)
    return Audit(checksum, copied_digest, counts, totals, tuple(sorted(set(blockers))))


def verify_backup_integrity(backup: Path, audit: Audit) -> None:
    if _file_digest(backup) != audit.backup_sha256:
        raise MigrationBlocked("backup hash changed")
    with closing(sqlite3.connect(backup)) as db, db:
        if _logical_digest(db) != audit.logical_sha256:
            raise MigrationBlocked("backup rows changed")


def verify_backup(backup: Path, audit: Audit) -> None:
    verify_backup_integrity(backup, audit)
    if not audit.can_import:
        raise MigrationBlocked("unmapped or inconsistent source records")


def validate_cloud_schema(cloud_schema: Path) -> None:
    """Compile the checked-in destination DDL without touching an operating D1."""
    with closing(sqlite3.connect(":memory:")) as db, db:
        try:
            db.executescript(Path(cloud_schema).read_text(encoding="utf-8"))
        except sqlite3.Error as exc:
            raise MigrationBlocked("destination schema invalid") from exc
        for table, expected in TARGET_COLUMNS.items():
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not expected <= columns:
                raise MigrationBlocked(f"destination schema missing:{table}")


def _iso(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise MigrationBlocked("timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationBlocked("timestamp invalid") from exc
    # The source schema's UTCDateTime stores UTC without a suffix in SQLite.
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(
        timezone.utc).isoformat().replace("+00:00", "Z")


def import_supported_test_subset(backup: Path, audit: Audit, target: Path,
                                 cloud_schema: Path) -> dict[str, int]:
    """Atomically import mapped synthetic rows to a new, disposable SQLite D1 analogue.

    It intentionally refuses any source rows lacking a lossless cloud mapping.
    This is not a production D1 upload routine.
    """
    verify_backup(backup, audit)
    validate_cloud_schema(cloud_schema)
    target = Path(target)
    if target.exists() or target.resolve() == Path(backup).resolve():
        raise MigrationBlocked("distinct new test target required")
    with closing(sqlite3.connect(backup)) as source, source, closing(sqlite3.connect(target)) as destination, destination:
        source.row_factory = sqlite3.Row
        destination.execute("PRAGMA foreign_keys = ON")
        destination.executescript(Path(cloud_schema).read_text(encoding="utf-8"))
        destination.execute("BEGIN IMMEDIATE")
        try:
            accounts = {row["id"]: row for row in source.execute(
                "SELECT * FROM broker_accounts")}
            instruments = {row["id"]: row for row in source.execute(
                "SELECT * FROM instruments")}
            if len({row["account_fingerprint"] for row in accounts.values()}) != len(accounts):
                raise MigrationBlocked("fingerprint collision across brokers")
            for row in accounts.values():
                # Source labels are not copied until an explicit masking policy exists.
                if row["masked_label"] not in (None, ""):
                    raise MigrationBlocked("account label mapping requires review")
                destination.execute("INSERT INTO cloud_accounts VALUES (?,?,?,?)",
                                    (row["account_fingerprint"], row["broker"], None,
                                     row["base_currency"]))
            revisions: dict[int, list[sqlite3.Row]] = {}
            for row in source.execute("SELECT * FROM recurring_buy_revisions ORDER BY plan_id, revision_no"):
                revisions.setdefault(row["plan_id"], []).append(row)
            for plan in source.execute("SELECT * FROM recurring_buy_plans ORDER BY id"):
                account = accounts.get(plan["account_id"]) if plan["account_id"] is not None else None
                instrument = instruments.get(plan["instrument_id"])
                if instrument is None or (plan["account_id"] is not None and account is None):
                    raise MigrationBlocked("plan source reference missing")
                fingerprint = account["account_fingerprint"] if account else None
                destination.execute("""INSERT INTO cloud_plans
                    (plan_key,account_fingerprint,source,market,ticker,asset_type,
                     instrument_currency,amount_currency,daily_amount,target_weight,
                     enabled,timezone_name,started_at,memo,version)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan["plan_key"], fingerprint, plan["source"], instrument["market"],
                     instrument["ticker"], instrument["asset_type"], instrument["currency"],
                     plan["currency"], str(plan["daily_amount"]),
                     None if plan["target_weight"] is None else str(plan["target_weight"]),
                     plan["enabled"], plan["timezone_name"], _iso(plan["started_at"]),
                     plan["memo"], plan["version"]))
                history = revisions.pop(plan["id"], [])
                for index, revision in enumerate(history, start=1):
                    identity = instruments.get(revision["instrument_id"])
                    revision_account = accounts.get(revision["account_id"]) if revision["account_id"] is not None else None
                    if identity is None or (revision["account_id"] is not None and revision_account is None) or revision["revision_no"] != index:
                        raise MigrationBlocked("plan revision reference or order invalid")
                    destination.execute("""INSERT INTO cloud_plan_revisions
                        (plan_key,revision_no,change_hash,source,account_fingerprint,
                         market,ticker,asset_type,instrument_currency,enabled,daily_amount,
                         amount_currency,target_weight,timezone_name,started_at,effective_from,memo)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (plan["plan_key"], revision["revision_no"], revision["change_hash"],
                         revision["source"], revision_account["account_fingerprint"] if revision_account else None,
                         identity["market"], identity["ticker"], identity["asset_type"],
                         identity["currency"], revision["enabled"], str(revision["daily_amount"]),
                         revision["currency"], None if revision["target_weight"] is None else str(revision["target_weight"]),
                         revision["timezone_name"], _iso(revision["started_at"]),
                         _iso(revision["effective_from"]), revision["memo"]))
            if revisions:
                raise MigrationBlocked("orphan plan revisions")
            for row in source.execute("SELECT * FROM transactions ORDER BY id"):
                account = accounts.get(row["account_id"])
                identity = instruments.get(row["instrument_id"]) if row["instrument_id"] is not None else None
                if account is None or (row["instrument_id"] is not None and identity is None):
                    raise MigrationBlocked("ledger source reference missing")
                destination.execute("""INSERT INTO cloud_transactions
                    (account_fingerprint,source_key,source,market,ticker,asset_type,
                     currency,transaction_type,status,traded_at,settled_at,quantity,
                     unit_price,gross_amount,fee,tax,net_amount)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (account["account_fingerprint"], row["source_key"], account["broker"],
                     identity["market"] if identity else None,
                     identity["ticker"] if identity else None,
                     identity["asset_type"] if identity else None,
                     row["currency"], row["transaction_type"], row["status"],
                     _iso(row["traded_at"]), _iso(row["settled_at"]) if row["settled_at"] else None,
                     str(row["quantity"]), *(None if row[field] is None else str(row[field])
                       for field in ("unit_price", "gross_amount", "fee", "tax", "net_amount"))))
            counts = {table: destination.execute(f'SELECT count(*) FROM cloud_{table}').fetchone()[0]
                      for table in ("accounts", "plans", "plan_revisions", "transactions")}
            expected = {"accounts": audit.counts["broker_accounts"],
                        "plans": audit.counts["recurring_buy_plans"],
                        "plan_revisions": audit.counts["recurring_buy_revisions"],
                        "transactions": audit.counts["transactions"]}
            if counts != expected or destination.execute("PRAGMA foreign_key_check").fetchone():
                raise MigrationBlocked("test target reconciliation failed")
            destination_totals = {}
            destination_totals.update(_amount_totals(destination, "cloud_plans",
                                                  "daily_amount", "plan_daily",
                                                  "amount_currency"))
            destination_totals.update(_amount_totals(destination, "cloud_transactions",
                                                  "gross_amount", "transaction_gross"))
            if destination_totals != audit.totals:
                raise MigrationBlocked("test target amount reconciliation failed")
            source_keys = {row[0] for row in source.execute(
                "SELECT plan_key FROM recurring_buy_plans")}
            target_keys = {row[0] for row in destination.execute(
                "SELECT plan_key FROM cloud_plans")}
            if source_keys != target_keys:
                raise MigrationBlocked("test target plan identity reconciliation failed")
            destination.commit()
            return counts
        except Exception:
            destination.rollback()
            raise


def _schema_objects(db: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [tuple(row) for row in db.execute("""SELECT type, name, tbl_name, sql
        FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        AND type IN ('table','index','view','trigger') ORDER BY type, name""")]


def _schema_digest(objects: list[tuple[str, str, str, str]]) -> str:
    return hashlib.sha256(json.dumps(objects, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _encode_row(row: tuple) -> str:
    values = []
    for value in row:
        if value is None:
            values.append(["null", None])
        elif isinstance(value, bytes):
            values.append(["blob", b64encode(value).decode("ascii")])
        elif isinstance(value, str):
            values.append(["text", value])
        elif isinstance(value, int):
            values.append(["integer", str(value)])
        elif isinstance(value, float):
            values.append(["real", repr(value)])
        else:
            raise MigrationBlocked("unsupported SQLite storage class")
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _decode_row(encoded: str) -> tuple:
    decoders = {"null": lambda _: None, "blob": b64decode, "text": str,
                "integer": int, "real": float}
    values = json.loads(encoded)
    if not isinstance(values, list):
        raise MigrationBlocked("invalid archived row")
    try:
        return tuple(decoders[kind](value) for kind, value in values)
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationBlocked("invalid archived storage class") from exc


def _archive_content(source: sqlite3.Connection) -> tuple[list, list, list, str]:
    objects = _schema_objects(source)
    rows = []
    columns = []
    for table in sorted(_archive_tables(source)):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", table):
            raise MigrationBlocked("unsafe source table name")
        columns.extend((table, ordinal, item[1]) for ordinal, item in enumerate(
            source.execute(f'PRAGMA table_info("{table}")')))
        for ordinal, row in enumerate(source.execute(f'SELECT * FROM "{table}" ORDER BY rowid')):
            encoded = _encode_row(row)
            rows.append((table, ordinal, encoded,
                         hashlib.sha256(encoded.encode()).hexdigest()))
    return objects, rows, columns, _schema_digest(objects)


def build_d1_archive_bundle(backup: Path, audit: Audit,
                            max_items: int = 50) -> dict:
    """Build bounded chunks in memory; never publish or transmit them here.

    This bundle contains private source rows when called with real data and
    requires separate transfer authorization and a secure destination.
    """
    if not isinstance(max_items, int) or max_items < 1 or max_items > 50:
        raise MigrationBlocked("archive chunk size must be between 1 and 50")
    verify_backup_integrity(backup, audit)
    with closing(sqlite3.connect(backup)) as source, source:
        if source.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationBlocked("source foreign key violation")
        objects, rows, columns, schema_hash = _archive_content(source)
        items = [
            {"type": "schema", "objectType": kind, "objectName": name,
             "table": table, "sql": sql}
            for kind, name, table, sql in objects
        ]
        items.extend({"type": "column", "table": table, "ordinal": ordinal,
                      "name": name} for table, ordinal, name in columns)
        items.extend({"type": "row", "table": table, "ordinal": ordinal,
                      "encoded": encoded, "sha": digest}
                     for table, ordinal, encoded, digest in rows)
        chunks = []
        for index, start in enumerate(range(0, len(items), max_items)):
            content = items[start:start + max_items]
            encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            chunks.append({"index": index, "items": content,
                           "sha": hashlib.sha256(encoded.encode()).hexdigest()})
        if not chunks:
            raise MigrationBlocked("empty archive schema cannot be transferred")
        chunk_hashes = [chunk["sha"] for chunk in chunks]
        root = hashlib.sha256(json.dumps(chunk_hashes,
                                       separators=(",", ":")).encode()).hexdigest()
        manifest = {"runId": f"legacy:{audit.logical_sha256}",
                    "sourceLogicalSha": audit.logical_sha256,
                    "backupSha": audit.backup_sha256,
                    "schemaSha": schema_hash,
                    "chunkRootSha": root,
                    "tableCount": len(_archive_tables(source)),
                    "rowCount": len(rows),
                    "expectedChunks": len(chunks)}
        return {"manifest": manifest, "chunks": chunks}


def _check_archive_schema(db: sqlite3.Connection) -> None:
    for table in ("cloud_legacy_archive_runs", "cloud_legacy_archive_schema",
                  "cloud_legacy_archive_rows", "cloud_legacy_archive_columns"):
        if table not in _tables(db):
            raise MigrationBlocked("legacy archive destination schema missing")


def archive_legacy_test_db(backup: Path, audit: Audit, target: Path,
                           cloud_schema: Path) -> dict[str, int | bool]:
    """Preserve all source SQLite rows in inert D1-style tables on a local test DB.

    This archive is restorable, but is not an operating query/ledger mapping.
    It never places old deliveries or occurrences into active Worker tables.
    """
    verify_backup_integrity(backup, audit)
    validate_cloud_schema(cloud_schema)
    target = Path(target)
    if target.resolve() == Path(backup).resolve():
        raise MigrationBlocked("distinct archive target required")
    with closing(sqlite3.connect(backup)) as source, source, closing(sqlite3.connect(target)) as destination, destination:
        if source.execute("PRAGMA foreign_key_check").fetchone():
            raise MigrationBlocked("source foreign key violation")
        destination.execute("PRAGMA foreign_keys = ON")
        destination.executescript(Path(cloud_schema).read_text(encoding="utf-8"))
        _check_archive_schema(destination)
        for active in ("cloud_outbox", "cloud_transactions", "cloud_plans",
                       "cloud_report_claims", "cloud_update_claims"):
            if destination.execute(f'SELECT 1 FROM "{active}" LIMIT 1').fetchone():
                raise MigrationBlocked("active operating target is not an archive test DB")
        objects, rows, columns, schema_hash = _archive_content(source)
        run_id = f"legacy:{audit.logical_sha256}"
        prior = destination.execute("""SELECT source_logical_sha256,
            source_backup_sha256, schema_sha256, table_count, row_count, status
            FROM cloud_legacy_archive_runs WHERE run_id=?""", (run_id,)).fetchone()
        expected = (audit.logical_sha256, audit.backup_sha256, schema_hash,
                    len(_archive_tables(source)), len(rows), "VERIFIED")
        if prior:
            if prior != expected or destination.execute("""SELECT count(*)
                FROM cloud_legacy_archive_rows WHERE run_id=?""", (run_id,)).fetchone()[0] != len(rows):
                raise MigrationBlocked("archive replay mismatch")
            archived_schema = destination.execute("""SELECT object_type, object_name,
                table_name, create_sql FROM cloud_legacy_archive_schema
                WHERE run_id=? ORDER BY object_type, object_name""", (run_id,)).fetchall()
            if _schema_digest(archived_schema) != schema_hash:
                raise MigrationBlocked("archive replay schema mismatch")
            archived_columns = destination.execute("""SELECT table_name, column_ordinal,
                column_name FROM cloud_legacy_archive_columns WHERE run_id=?
                ORDER BY table_name, column_ordinal""", (run_id,)).fetchall()
            if archived_columns != columns:
                raise MigrationBlocked("archive replay columns mismatch")
            for table, ordinal, _, digest in rows:
                stored = destination.execute("""SELECT row_sha256, encoded_values
                    FROM cloud_legacy_archive_rows WHERE run_id=? AND table_name=?
                    AND row_ordinal=?""", (run_id, table, ordinal)).fetchone()
                if not stored or stored[0] != digest or hashlib.sha256(
                    stored[1].encode()).hexdigest() != digest:
                    raise MigrationBlocked("archive replay row mismatch")
            return {"tables": expected[3], "rows": expected[4], "replay": True}
        if destination.execute("SELECT 1 FROM cloud_legacy_archive_runs LIMIT 1").fetchone():
            raise MigrationBlocked("different archive already present")
        destination.execute("BEGIN IMMEDIATE")
        try:
            destination.execute("""INSERT INTO cloud_legacy_archive_runs
                VALUES (?,?,?,?,?,?,?)""", (run_id, *expected[:-1], "STAGING"))
            destination.executemany("""INSERT INTO cloud_legacy_archive_schema
                VALUES (?,?,?,?,?)""", ((run_id, *item) for item in objects))
            destination.executemany("""INSERT INTO cloud_legacy_archive_rows
                VALUES (?,?,?,?,?)""", ((run_id, *item) for item in rows))
            destination.executemany("""INSERT INTO cloud_legacy_archive_columns
                VALUES (?,?,?,?)""", ((run_id, *item) for item in columns))
            if destination.execute("SELECT count(*) FROM cloud_legacy_archive_rows").fetchone()[0] != len(rows):
                raise MigrationBlocked("archive row count mismatch")
            if destination.execute("PRAGMA foreign_key_check").fetchone():
                raise MigrationBlocked("archive reference failed")
            destination.execute("""UPDATE cloud_legacy_archive_runs SET status='VERIFIED'
                WHERE run_id=? AND status='STAGING'""", (run_id,))
            destination.commit()
        except Exception:
            destination.rollback()
            raise
        return {"tables": expected[3], "rows": expected[4], "replay": False}


def restore_legacy_test_db(archive: Path, restore: Path) -> None:
    """Restore an inert archive; never discard newer active cloud state implicitly."""
    restore = Path(restore)
    if restore.exists() or restore.resolve() == Path(archive).resolve():
        raise MigrationBlocked("distinct new restore path required")
    temporary = restore.with_name(f"{restore.name}.partial")
    if temporary.exists():
        raise MigrationBlocked("partial restore path already exists")
    try:
        # A sqlite3 transaction context does not close the connection. Explicit
        # closing is required before Windows rename/unlink can touch the file.
        with closing(sqlite3.connect(archive)) as stored, closing(
            sqlite3.connect(temporary)
        ) as output, output:
            _check_archive_schema(stored)
            tables = _tables(stored)
            for table in ROLLBACK_STATE_TABLES:
                if table in tables and stored.execute(
                    f'SELECT 1 FROM "{table}" LIMIT 1'
                ).fetchone():
                    raise MigrationBlocked("active cloud state requires reconciled rollback")
            run = stored.execute("""SELECT run_id, source_logical_sha256,
                schema_sha256, table_count, row_count, status
                FROM cloud_legacy_archive_runs""").fetchall()
            if len(run) != 1 or run[0][-1] != "VERIFIED":
                raise MigrationBlocked("verified archive required")
            run_id, logical_hash, schema_hash, table_count, row_count, _ = run[0]
            objects = stored.execute("""SELECT object_type, object_name, table_name, create_sql
                FROM cloud_legacy_archive_schema WHERE run_id=? ORDER BY object_type, object_name""",
                (run_id,)).fetchall()
            if _schema_digest(objects) != schema_hash:
                raise MigrationBlocked("archived schema changed")
            for object_type, _, _, sql in objects:
                if object_type == "table":
                    output.execute(sql)
            archived = stored.execute("""SELECT table_name, row_ordinal, encoded_values,
                row_sha256 FROM cloud_legacy_archive_rows WHERE run_id=?
                ORDER BY table_name, row_ordinal""", (run_id,)).fetchall()
            if len(archived) != row_count:
                raise MigrationBlocked("archived row count changed")
            ordinary = [item for item in archived if item[0] != "sqlite_sequence"]
            sequence = [item for item in archived if item[0] == "sqlite_sequence"]
            sequence_cleared = False
            for table, _, encoded, digest in ordinary + sequence:
                if table == "sqlite_sequence" and not sequence_cleared:
                    output.execute("DELETE FROM sqlite_sequence")
                    sequence_cleared = True
                if not re.fullmatch(r"[a-z][a-z0-9_]*", table) or hashlib.sha256(
                    encoded.encode()).hexdigest() != digest:
                    raise MigrationBlocked("archived row changed")
                values = _decode_row(encoded)
                placeholders = ",".join("?" for _ in values)
                output.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', values)
            for kind in ("index", "view", "trigger"):
                for object_type, _, _, sql in objects:
                    if object_type == kind:
                        output.execute(sql)
            if len(_archive_tables(output)) != table_count or _logical_digest(output) != logical_hash:
                raise MigrationBlocked("restored data differs")
            if output.execute("PRAGMA foreign_key_check").fetchone() or output.execute(
                "PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationBlocked("restored integrity check failed")
            output.commit()
        if restore.exists():
            raise MigrationBlocked("restore destination appeared during reconciliation")
        temporary.replace(restore)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
