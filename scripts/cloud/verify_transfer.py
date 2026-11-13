"""Offline manifest and verification for a frozen SQLite backup and snapshots.

This tool never reads the operating database unless the operator explicitly
passes it. Create a manifest from a SQLite online backup and two Meritz JSON
snapshots, then verify copied files against that manifest before starting a
new runtime. The manifest contains hashes and sizes, not account contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

_REQUIRED_TABLES = frozenset({
    "recurring_buy_plans", "recurring_buy_revisions", "recurring_buy_occurrences",
    "portfolio_snapshots", "snapshot_sources", "transactions",
    "reports", "report_deliveries",
})


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_ok(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError("database must be an existing regular file")
    # immutable=1 prevents a silently incomplete WAL replay. Only pass a
    # finalized SQLite backup, never the live database or an uncheckpointed copy.
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("database foreign key check failed")
        table_names = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not _REQUIRED_TABLES.issubset(table_names):
            raise ValueError("required account-bot tables are missing")


def _snapshot_ok(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError("snapshot must be an existing regular file")
    with path.open("r", encoding="utf-8") as source:
        if not isinstance(json.load(source), dict):
            raise ValueError("snapshot root must be an object")


def manifest_for(
    db: Path, meritz: Path, accumul: Path, *,
    kiwoom: Path | None = None, toss: Path | None = None,
) -> dict[str, object]:
    entries = {"database": db, "meritz": meritz, "meritz_accumul": accumul}
    if kiwoom is not None:
        entries["kiwoom"] = kiwoom
    if toss is not None:
        entries["toss"] = toss
    for label, path in entries.items():
        (_database_ok if label == "database" else _snapshot_ok)(path)
    return {
        "format": 1,
        "files": {
            label: {"sha256": _digest(path), "size": path.stat().st_size}
            for label, path in entries.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--meritz", required=True, type=Path)
    parser.add_argument("--meritz-accumul", required=True, type=Path)
    parser.add_argument("--kiwoom", type=Path, help="optional configured Kiwoom fallback JSON")
    parser.add_argument("--toss", type=Path, help="optional configured Toss fallback JSON")
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        actual = manifest_for(
            args.db, args.meritz, args.meritz_accumul,
            kiwoom=args.kiwoom, toss=args.toss,
        )
        if args.mode == "create":
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(args.manifest, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(actual, destination, sort_keys=True)
                destination.write("\n")
            print("transfer manifest created")
        else:
            if args.manifest.is_symlink():
                raise ValueError("manifest must not be a symbolic link")
            with args.manifest.open("r", encoding="utf-8") as source:
                expected = json.load(source)
            if expected != actual:
                raise ValueError("transferred files differ from manifest")
            print("database and configured snapshots verified")
    except (OSError, sqlite3.DatabaseError, UnicodeError, json.JSONDecodeError, ValueError):
        print("transfer verification failed; inspect local input paths and files")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
