"""Small D1 binding adapter for future webhook/Cron claims and send outbox.

The database must already contain deploy/cloudflare/schema.sql. No actual
Worker, Telegram send, or operating database is accessed by this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import uuid4


class Statement(Protocol):
    def bind(self, *values: object) -> Statement: ...

    async def run(self) -> Any: ...


class D1Binding(Protocol):
    def prepare(self, query: str) -> Statement: ...

    async def batch(self, statements: Sequence[Statement]) -> Sequence[Any]: ...


def _changes(result: Any) -> int:
    meta = result["meta"] if isinstance(result, dict) else result.meta
    changes = meta["changes"] if isinstance(meta, dict) else meta.changes
    if isinstance(changes, bool) or not isinstance(changes, int):
        raise ValueError("D1 result has no valid change count")
    return changes


def _outbox_statement(
    db: D1Binding, key: str, destination: str, payload: str, now: str,
    *, claim_table: str, claim_column: str, claim_key: int | str, nonce: str,
):
    if (claim_table, claim_column) not in {
        ("cloud_update_claims", "update_id"), ("cloud_report_claims", "period_key")
    }:
        raise ValueError("unsupported claim table")
    return db.prepare(
        "INSERT INTO cloud_outbox "
        "(event_key, destination_key, payload_json, created_at) "
        f"SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM {claim_table} "
        f"WHERE {claim_column} = ? AND claim_nonce = ?)"
    ).bind(key, destination, payload, now, claim_key, nonce)


async def enqueue_update(
    db: D1Binding, *, update_id: int, destination: str, payload_json: str, now: str
) -> bool:
    """Atomically claim an update and stage one response; duplicate is no-op."""
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id <= 0:
        raise ValueError("invalid update ID")
    if not destination or not payload_json or not now:
        raise ValueError("missing outbox input")
    nonce = uuid4().hex
    results = await db.batch([
        db.prepare(
            "INSERT INTO cloud_update_claims (update_id, received_at, claim_nonce) "
            "VALUES (?, ?, ?) ON CONFLICT(update_id) DO NOTHING"
        ).bind(update_id, now, nonce),
        _outbox_statement(
            db, f"update:{update_id}:0", destination, payload_json, now,
            claim_table="cloud_update_claims", claim_column="update_id",
            claim_key=update_id, nonce=nonce,
        ),
    ])
    if len(results) != 2:
        raise ValueError("D1 batch result is incomplete")
    first, second = (_changes(result) for result in results)
    if first != second or first not in (0, 1):
        raise ValueError("D1 claim and outbox state disagree")
    return first == 1


async def enqueue_report(
    db: D1Binding, *, period_key: str, deliveries: Sequence[tuple[str, str]], now: str
) -> bool:
    """Atomically claim a period and stage one message per distinct recipient."""
    if not period_key or not now or not deliveries:
        raise ValueError("missing report input")
    destinations = [destination for destination, _ in deliveries]
    if len(set(destinations)) != len(destinations) or any(not item for item in destinations):
        raise ValueError("report destinations must be distinct and nonempty")
    if any(not payload for _, payload in deliveries):
        raise ValueError("report payload is empty")
    nonce = uuid4().hex
    statements = [db.prepare(
        "INSERT INTO cloud_report_claims (period_key, claimed_at, claim_nonce) "
        "VALUES (?, ?, ?) ON CONFLICT(period_key) DO NOTHING"
    ).bind(period_key, now, nonce)]
    statements.extend(
        _outbox_statement(
            db, f"report:{period_key}:{index}", dest, payload, now,
            claim_table="cloud_report_claims", claim_column="period_key",
            claim_key=period_key, nonce=nonce,
        )
        for index, (dest, payload) in enumerate(deliveries)
    )
    results = await db.batch(statements)
    if len(results) != len(statements):
        raise ValueError("D1 batch result is incomplete")
    changes = [_changes(result) for result in results]
    if changes[0] not in (0, 1) or any(value != changes[0] for value in changes[1:]):
        raise ValueError("D1 report claim and outbox state disagree")
    return changes[0] == 1


async def begin_send(db: D1Binding, event_key: str) -> bool:
    """Reserve a pending send. A crash leaves SENDING for manual reconciliation."""
    if not event_key:
        raise ValueError("event key is empty")
    result = await db.prepare(
        "UPDATE cloud_outbox SET status = 'SENDING' "
        "WHERE event_key = ? AND status = 'PENDING'"
    ).bind(event_key).run()
    return _changes(result) == 1


async def finish_send(db: D1Binding, event_key: str, *, sent: bool, now: str) -> bool:
    """Record a definite success or uncertain result; never auto-retry UNKNOWN."""
    if not event_key or not now:
        raise ValueError("missing send result")
    status = "SENT" if sent else "UNKNOWN"
    result = await db.prepare(
        "UPDATE cloud_outbox SET status = ?, finished_at = ? "
        "WHERE event_key = ? AND status = 'SENDING'"
    ).bind(status, now, event_key).run()
    return _changes(result) == 1
