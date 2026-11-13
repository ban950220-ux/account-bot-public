"""Fail closed before PTB's polling bootstrap can remove an existing webhook."""

from __future__ import annotations

from contextlib import closing


async def require_polling_ownership(bot) -> None:
    """Inspect only: never delete a webhook or assume ownership after a failed read.

    This is a startup safeguard, not a distributed lease. Approved cutover still
    requires stopping the PC process before activating a cloud webhook/Cron.
    """
    try:
        info = await bot.get_webhook_info()
    except Exception:
        # Do not expose transport errors containing a token or webhook secret.
        raise RuntimeError("polling ownership could not be verified") from None
    if not isinstance(getattr(info, "url", None), str) or info.url:
        raise RuntimeError("polling blocked: webhook present or ownership unknown")


def require_rollback_clear(database_path, *, now=None) -> None:
    """Read-only startup interlock for a generated rollback review copy.

    There is intentionally no automatic release path. Operator reconciliation
    must produce a cleared proof in a separate, reviewed release operation.
    """
    import json
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path

    if database_path is None:
        return
    path = Path(database_path)
    if not path.exists():
        return
    if path.is_symlink():
        raise RuntimeError("rollback database identity unavailable")
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db, db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cloud_rollback_gate'").fetchone():
                return
            rows = db.execute("SELECT state,resume_after,proof_json FROM cloud_rollback_gate").fetchall()
            if len(rows) != 1:
                raise ValueError("gate")
            state, resume_after, proof_json = rows[0]
            proof = json.loads(proof_json) if proof_json else {}
            instant = now or datetime.now(timezone.utc)
            if (state != "CLEARED" or not isinstance(resume_after, str) or
                    datetime.fromisoformat(resume_after.replace("Z", "+00:00")) > instant or
                    proof.get("cloudStopped") is not True or
                    proof.get("webhookDetached") is not True or
                    proof.get("deliveryStatesReconciled") is not True):
                raise ValueError("held")
    except (sqlite3.Error, ValueError, TypeError, OSError):
        raise RuntimeError("rollback startup held until cloud and delivery reconciliation") from None
