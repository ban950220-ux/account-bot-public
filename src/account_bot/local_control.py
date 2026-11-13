"""Same-user file control for the windowless Telegram runtime.

No network listener or secret is added. The existing runtime lock is the source
of truth for whether a process is alive; status files are only diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from account_bot.runtime_lock import runtime_is_running

STATUS_NAME = "account_bot.status.json"
REQUEST_PREFIX = "account_bot.stop"
LEASE_PREFIX = "account_bot.run"


def _request_path(data_dir: Path, run_id: str) -> Path:
    return data_dir / f"{REQUEST_PREFIX}.{run_id}.json"


def _lease_path(data_dir: Path, run_id: str) -> Path:
    return data_dir / f"{LEASE_PREFIX}.{run_id}.lock"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_status(path: Path) -> dict[str, Any] | None:
    value = _read_json(path)
    if value is None:
        return None
    run_id = value.get("run_id")
    source_hash = value.get("weekly_report_sha256_at_start")
    runtime_hash = value.get("runtime_source_sha256_at_start")
    pid = value.get("pid")
    started_at = value.get("started_at")
    package_version = value.get("package_version")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        return None
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        return None
    if not isinstance(runtime_hash, str) or re.fullmatch(r"[0-9a-f]{64}", runtime_hash) is None:
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(started_at, str) or len(started_at) > 64:
        return None
    try:
        datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if (
        not isinstance(package_version, str)
        or re.fullmatch(r"[A-Za-z0-9_.+-]{1,40}", package_version) is None
    ):
        return None
    if value.get("state") not in {"starting", "running", "stopping", "stopped", "cleanup_failed"}:
        return None
    return value


def local_status(data_dir: Path) -> dict[str, Any]:
    """Trust runtime metadata only while both its main and per-run locks are held."""
    running = runtime_is_running(data_dir / "account_bot.lock")
    status = _read_status(data_dir / STATUS_NAME)
    if running and (
        status is None or not runtime_is_running(_lease_path(data_dir, status["run_id"]))
    ):
        status = None
    return {"running": running, "status": status}


def request_stop(data_dir: Path) -> str:
    """Target one observed runtime, never a future process with the same path."""
    snapshot = local_status(data_dir)
    if not snapshot["running"]:
        raise RuntimeError("account-bot is not running")
    status = snapshot["status"]
    if not isinstance(status, dict) or status.get("state") not in {"starting", "running"}:
        raise RuntimeError("running account-bot has no ready control status")
    run_id = status.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise RuntimeError("running account-bot has invalid control status")
    request_path = _request_path(data_dir, run_id)
    if request_path.is_symlink():
        raise RuntimeError("local stop request path is invalid")
    prior = _read_json(request_path)
    if request_path.exists() and prior != {"run_id": run_id}:
        raise RuntimeError("local stop request is invalid")
    if prior is None:
        _atomic_json(request_path, {"run_id": run_id})
    return run_id


class LocalRuntimeControl:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.accepting = True
        self.state = "starting"
        package_dir = Path(__file__).parent
        report_source = package_dir / "reports" / "periodic.py"
        self.report_sha256 = hashlib.sha256(report_source.read_bytes()).hexdigest()
        source_digest = hashlib.sha256()
        for relative_name in (
            "app.py",
            "cli.py",
            "local_control.py",
            "runtime_lock.py",
            "scheduler/jobs.py",
            "telegram_ui/auth.py",
            "telegram_ui/handlers.py",
            "reports/periodic.py",
        ):
            source_digest.update(relative_name.encode("utf-8") + b"\0")
            source_digest.update((package_dir / relative_name).read_bytes())
        self.runtime_sha256 = source_digest.hexdigest()
        try:
            self.package_version = version("account-bot")
        except PackageNotFoundError:
            self.package_version = "source"

    @property
    def lease_path(self) -> Path:
        return _lease_path(self.data_dir, self.run_id)

    def _publish(self, state: str) -> None:
        self.state = state
        _atomic_json(
            self.data_dir / STATUS_NAME,
            {
                "run_id": self.run_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "state": state,
                "package_version": self.package_version,
                "weekly_report_sha256_at_start": self.report_sha256,
                "runtime_source_sha256_at_start": self.runtime_sha256,
            },
        )

    def start(self) -> None:
        self._publish("starting")

    def finish(self, *, clean: bool = True) -> None:
        self.accepting = False
        self._publish("stopped" if clean else "cleanup_failed")

    async def poll(self, context: Any) -> None:
        if self.state == "starting":
            self._publish("running")
        request = _read_json(_request_path(self.data_dir, self.run_id))
        if request is None or request.get("run_id") != self.run_id or not self.accepting:
            return
        self.accepting = False
        try:
            self._publish("stopping")
        finally:
            # PTB's run_polling shutdown stops fetching updates, then drains
            # queued handlers and waits for JobQueue jobs before post_shutdown.
            context.application.stop_running()
