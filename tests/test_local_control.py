from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from account_bot.cli import main, parser
from account_bot.local_control import (
    REQUEST_PREFIX,
    STATUS_NAME,
    LocalRuntimeControl,
    local_status,
    request_stop,
)
from account_bot.runtime_lock import AlreadyRunningError, single_instance


def test_status_does_not_create_a_lock_and_stop_rejects_absent_runtime() -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        assert local_status(directory) == {"running": False, "status": None}
        with pytest.raises(RuntimeError, match="not running"):
            request_stop(directory)
        assert not list(directory.iterdir())


@pytest.mark.asyncio
async def test_stop_targets_only_active_run_and_preserves_stale_request() -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        control = LocalRuntimeControl(directory)
        report_path = Path(__file__).resolve().parents[1] / "src/account_bot/reports/periodic.py"
        assert control.report_sha256 == hashlib.sha256(report_path.read_bytes()).hexdigest()
        stopped = 0

        def stop_running() -> None:
            nonlocal stopped
            stopped += 1

        context = SimpleNamespace(application=SimpleNamespace(stop_running=stop_running))
        with (
            single_instance(directory / "account_bot.lock"),
            single_instance(control.lease_path),
        ):
            control.start()
            snapshot = local_status(directory)
            assert snapshot["running"] is True
            assert snapshot["status"]["run_id"] == control.run_id
            assert snapshot["status"]["weekly_report_sha256_at_start"] == (
                control.report_sha256
            )
            assert snapshot["status"]["runtime_source_sha256_at_start"] == (
                control.runtime_sha256
            )
            stale = directory / f"{REQUEST_PREFIX}.previous-run.json"
            stale.write_text('{"run_id":"previous-run"}', encoding="utf-8")
            await control.poll(context)
            assert control.accepting and stopped == 0
            assert stale.exists()

            assert request_stop(directory) == control.run_id
            request_path = directory / f"{REQUEST_PREFIX}.{control.run_id}.json"
            assert json.loads(request_path.read_text())["run_id"] == control.run_id
            assert request_stop(directory) == control.run_id
            assert stale.exists()
            await control.poll(context)
            assert stopped == 1
            assert control.state == "stopping" and not control.accepting
            await control.poll(context)
            assert stopped == 1
            control.finish()
            assert json.loads((directory / STATUS_NAME).read_text())["state"] == "stopped"
        assert local_status(directory)["running"] is False
        assert local_status(directory)["status"]["state"] == "stopped"


def test_legacy_runtime_lock_cannot_be_stopped_by_new_control_command() -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        with single_instance(directory / "account_bot.lock"):
            with pytest.raises(RuntimeError, match="no ready control status"):
                request_stop(directory)
        assert list(directory.glob(f"{REQUEST_PREFIX}.*.json")) == []


def test_local_cli_has_status_and_stop_commands() -> None:
    assert parser().parse_args(["status"]).command == "status"
    assert parser().parse_args(["stop"]).command == "stop"


def test_local_cli_targets_running_instance_without_loading_runtime(capsys) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        control = LocalRuntimeControl(directory)
        with (
            single_instance(directory / "account_bot.lock"),
            single_instance(control.lease_path),
        ):
            control.start()
            with patch(
                "account_bot.cli.Settings.load",
                return_value=SimpleNamespace(data_dir=directory),
            ):
                assert main(["status"]) == 0
                status_output = capsys.readouterr().out
                assert control.report_sha256 in status_output
                assert control.runtime_sha256 in status_output
                assert main(["stop"]) == 0
                assert control.run_id in capsys.readouterr().out
                control.finish()
                assert main(["status"]) == 0
                assert "finalizing" in capsys.readouterr().out
        assert (directory / f"{REQUEST_PREFIX}.{control.run_id}.json").is_file()


def test_invalid_status_cannot_target_a_running_legacy_process() -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / STATUS_NAME).write_text('{"run_id":"not-a-valid-id"}', encoding="utf-8")
        with single_instance(directory / "account_bot.lock"):
            assert local_status(directory)["status"] is None
            with pytest.raises(RuntimeError, match="no ready control status"):
                request_stop(directory)


def test_stale_valid_status_does_not_authorize_stop_of_another_lock_owner() -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        prior = LocalRuntimeControl(directory)
        prior.start()
        with single_instance(directory / "account_bot.lock"):
            assert local_status(directory) == {"running": True, "status": None}
            with pytest.raises(RuntimeError, match="no ready control status"):
                request_stop(directory)
        assert not (directory / f"{REQUEST_PREFIX}.{prior.run_id}.json").exists()


def test_duplicate_launcher_returns_failure_without_starting_a_second_bot(capsys) -> None:
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        settings = SimpleNamespace(
            data_dir=directory,
            log_dir=directory,
            ensure_runtime_directories=lambda: None,
        )

        @contextmanager
        def reject_second_start(path):
            del path
            raise AlreadyRunningError("fixture lock held")
            yield

        with (
            patch("account_bot.cli.Settings.load", return_value=settings),
            patch("account_bot.cli.configure_logging"),
            patch("account_bot.runtime_lock.single_instance", reject_second_start),
            patch("account_bot.app.run_polling") as polling,
        ):
            assert main(["run"]) == 2
            polling.assert_not_called()
        assert "duplicate start ignored" in capsys.readouterr().out
