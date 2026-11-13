"""Offline transfer checks use disposable SQLite and JSON fixtures only."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


def _tool():
    path = Path(__file__).resolve().parents[1] / "scripts/cloud/verify_transfer.py"
    spec = importlib.util.spec_from_file_location("verify_transfer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(tmp_path: Path):
    db = tmp_path / "backup.db"
    with closing(sqlite3.connect(db)) as connection, connection:
        for table in _tool()._REQUIRED_TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO recurring_buy_plans VALUES (1)")
    meritz = tmp_path / "snapshot.json"
    accumul = tmp_path / "recurring.json"
    meritz.write_text('{"as_of":"fixture"}', encoding="utf-8")
    accumul.write_text('{"plans":[]}', encoding="utf-8")
    return db, meritz, accumul


def _args(mode: str, files: tuple[Path, Path, Path], manifest: Path):
    db, meritz, accumul = files
    return [
        mode, "--db", str(db), "--meritz", str(meritz),
        "--meritz-accumul", str(accumul), "--manifest", str(manifest),
    ]


def test_transfer_manifest_round_trip_and_mutation_rejection(tmp_path: Path) -> None:
    tool = _tool()
    files = _inputs(tmp_path)
    manifest = tmp_path / "manifest.json"
    assert tool.main(_args("create", files, manifest)) == 0
    assert set(json.loads(manifest.read_text())["files"]) == {
        "database", "meritz", "meritz_accumul"
    }
    assert tool.main(_args("verify", files, manifest)) == 0
    files[2].write_text('{"plans":[1]}', encoding="utf-8")
    assert tool.main(_args("verify", files, manifest)) == 2


def test_transfer_rejects_invalid_database_and_snapshot(tmp_path: Path) -> None:
    tool = _tool()
    files = _inputs(tmp_path)
    files[0].write_bytes(b"not sqlite")
    assert tool.main(_args("create", files, tmp_path / "manifest.json")) == 2
    second = tmp_path / "second"
    second.mkdir()
    files = _inputs(second)
    files[1].write_text("[]", encoding="utf-8")
    assert tool.main(_args("create", files, tmp_path / "manifest.json")) == 2


def test_transfer_rejects_symlinked_input(tmp_path: Path) -> None:
    tool = _tool()
    db, meritz, accumul = _inputs(tmp_path)
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(meritz)
    except OSError:
        # Windows without Developer Mode or elevation cannot create symlinks.
        pytest.skip("symlink creation is not permitted on this host")
    assert tool.main(_args("create", (db, alias, accumul), tmp_path / "manifest.json")) == 2
