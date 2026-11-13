"""D1 claim SQL exercised against disposable SQLite with a transactional fake."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from account_bot.cloud_d1 import begin_send, enqueue_report, enqueue_update, finish_send


class FakeStatement:
    def __init__(self, db: FakeD1, query: str) -> None:
        self.db = db
        self.query = query
        self.values: tuple[object, ...] = ()

    def bind(self, *values: object) -> FakeStatement:
        self.values = values
        return self

    async def run(self) -> dict:
        return self.db.execute(self)


class FakeD1:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        schema = (
            Path(__file__).resolve().parents[1] / "deploy/cloudflare/schema.sql"
        ).read_text(encoding="utf-8")
        self.connection.executescript(schema)
        self.fail_after: int | None = None

    def prepare(self, query: str) -> FakeStatement:
        return FakeStatement(self, query)

    def execute(self, statement: FakeStatement) -> dict:
        before = self.connection.total_changes
        self.connection.execute(statement.query, statement.values)
        return {"meta": {"changes": self.connection.total_changes - before}}

    async def batch(self, statements: list[FakeStatement]) -> list[dict]:
        self.connection.execute("BEGIN")
        try:
            results = []
            for index, statement in enumerate(statements):
                if index == self.fail_after:
                    raise RuntimeError("synthetic batch failure")
                results.append(self.execute(statement))
            self.connection.execute("COMMIT")
            return results
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def rows(self, table: str) -> list[tuple]:
        assert table in {"cloud_update_claims", "cloud_report_claims", "cloud_outbox"}
        return self.connection.execute(f"SELECT * FROM {table}").fetchall()


@pytest.mark.asyncio
async def test_update_claim_outbox_atomic_and_duplicate_safe() -> None:
    db = FakeD1()
    inputs = dict(update_id=73, destination="fixture-recipient", payload_json='{"text":"hi"}', now="t1")
    assert await enqueue_update(db, **inputs)
    assert not await enqueue_update(db, **{**inputs, "payload_json": '{"text":"changed"}'})
    assert len(db.rows("cloud_update_claims")) == 1
    assert len(db.rows("cloud_outbox")) == 1
    assert db.rows("cloud_outbox")[0][2] == '{"text":"hi"}'


@pytest.mark.asyncio
async def test_report_claim_does_not_add_recipients_on_duplicate() -> None:
    db = FakeD1()
    assert await enqueue_report(
        db, period_key="daily:fixture", deliveries=[("one", "a"), ("two", "b")], now="t1"
    )
    assert not await enqueue_report(
        db, period_key="daily:fixture",
        deliveries=[("one", "changed"), ("two", "changed"), ("three", "c")], now="t2"
    )
    assert len(db.rows("cloud_report_claims")) == 1
    assert len(db.rows("cloud_outbox")) == 2


@pytest.mark.asyncio
async def test_failed_batch_rolls_back_claim_and_outbox() -> None:
    db = FakeD1()
    db.fail_after = 1
    with pytest.raises(RuntimeError, match="synthetic batch failure"):
        await enqueue_update(db, update_id=7, destination="one", payload_json="x", now="t")
    assert db.rows("cloud_update_claims") == []
    assert db.rows("cloud_outbox") == []


@pytest.mark.asyncio
async def test_unknown_delivery_is_never_reclaimed() -> None:
    db = FakeD1()
    assert await enqueue_update(
        db, update_id=3, destination="one", payload_json="x", now="t"
    )
    assert await begin_send(db, "update:3:0")
    assert not await begin_send(db, "update:3:0")
    assert await finish_send(db, "update:3:0", sent=False, now="t2")
    assert not await begin_send(db, "update:3:0")
    assert not await finish_send(db, "update:3:0", sent=True, now="t3")
    assert db.rows("cloud_outbox")[0][3] == "UNKNOWN"


@pytest.mark.asyncio
async def test_outbox_key_collision_rolls_back_new_claim() -> None:
    db = FakeD1()
    db.connection.execute(
        "INSERT INTO cloud_outbox "
        "(event_key, destination_key, payload_json, created_at) VALUES (?, ?, ?, ?)",
        ("update:9:0", "fixture", "old", "t0"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await enqueue_update(db, update_id=9, destination="fixture", payload_json="new", now="t1")
    assert db.rows("cloud_update_claims") == []
    assert db.rows("cloud_outbox")[0][2] == "old"


@pytest.mark.asyncio
async def test_report_batch_conflict_rolls_back_all_recipients() -> None:
    db = FakeD1()
    db.connection.execute(
        "INSERT INTO cloud_outbox "
        "(event_key, destination_key, payload_json, created_at) VALUES (?, ?, ?, ?)",
        ("report:weekly:fixture:1", "fixture", "old", "t0"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await enqueue_report(
            db, period_key="weekly:fixture", deliveries=[("one", "a"), ("two", "b")], now="t1"
        )
    assert db.rows("cloud_report_claims") == []
    assert len(db.rows("cloud_outbox")) == 1


@pytest.mark.asyncio
async def test_success_and_crash_left_sending_are_not_reclaimed() -> None:
    db = FakeD1()
    assert await enqueue_update(db, update_id=4, destination="one", payload_json="x", now="t")
    assert await begin_send(db, "update:4:0")
    assert not await begin_send(db, "update:4:0")
    assert db.rows("cloud_outbox")[0][3] == "SENDING"
    assert await finish_send(db, "update:4:0", sent=True, now="t2")
    assert not await begin_send(db, "update:4:0")
    assert db.rows("cloud_outbox")[0][3] == "SENT"
