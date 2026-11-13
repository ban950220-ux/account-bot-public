from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.config import Settings, SettingsError, load_dotenv_file  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_empty_environment_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(environ={}, base_dir=directory)

        self.assertEqual(settings.allowed_telegram_user_ids, frozenset())
        self.assertFalse(settings.telegram_enabled)
        self.assertFalse(settings.scheduler_enabled)
        self.assertFalse(settings.is_telegram_user_allowed(123456))
        self.assertEqual(settings.timezone_name, "Asia/Seoul")
        self.assertTrue(settings.database_url.startswith("sqlite+aiosqlite:///"))

    def test_dotenv_loads_and_process_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text(
                "ACCOUNT_BOT_TELEGRAM_TOKEN=file-secret\n"
                "ALLOWED_TELEGRAM_USER_IDS=111, 222\n"
                "ACCOUNT_BOT_DATA_DIR=runtime-data\n"
                "KIWOOM_SNAPSHOT_PATH=snapshots/kiwoom.json\n",
                encoding="utf-8",
            )
            settings = Settings.load(
                env_file=env_path,
                environ={"ACCOUNT_BOT_TELEGRAM_TOKEN": "process-secret"},
                base_dir=root,
            )

        self.assertEqual(settings.telegram_token, "process-secret")
        self.assertTrue(settings.is_telegram_user_allowed(111))
        self.assertFalse(settings.is_telegram_user_allowed(333))
        self.assertTrue(settings.data_dir.is_absolute())
        self.assertEqual(
            settings.kiwoom_snapshot_path,
            (root / "snapshots" / "kiwoom.json").resolve(),
        )

    def test_explicit_env_file_anchors_relative_paths_to_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text("MERITZ_SNAPSHOT_PATH=snapshots\n", encoding="utf-8")
            settings = Settings.load(env_file=env_path, environ={})

        self.assertEqual(settings.base_dir, root.resolve())
        self.assertEqual(settings.meritz_snapshot_path, (root / "snapshots").resolve())

    def test_separate_kiwoom_credentials_are_validated_and_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(
                environ={
                    "KIWOOM_MAIN_APPKEY": "main-key",
                    "KIWOOM_MAIN_SECRET": "main-secret",
                    "KIWOOM_ACCUM_APPKEY": "accum-key",
                    "KIWOOM_ACCUM_SECRET": "accum-secret",
                },
                base_dir=directory,
            )
        self.assertTrue(settings.credential_status()["kiwoom_main"])
        self.assertTrue(settings.credential_status()["kiwoom_accum"])
        for secret in ("main-key", "main-secret", "accum-key", "accum-secret"):
            self.assertNotIn(secret, repr(settings))

    def test_repr_never_contains_sensitive_values_or_user_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(
                environ={
                    "ACCOUNT_BOT_TELEGRAM_TOKEN": "telegram-super-secret",
                    "ALLOWED_TELEGRAM_USER_IDS": "987654321",
                    "KIWOOM_APP_KEY": "key-value",
                    "KIWOOM_APP_SECRET": "secret-value",
                    "ACCOUNT_BOT_FINGERPRINT_PEPPER": "pepper-value-long",
                },
                base_dir=directory,
            )
        rendered = repr(settings)
        for sensitive in (
            "telegram-super-secret",
            "987654321",
            "key-value",
            "secret-value",
            "pepper-value-long",
        ):
            self.assertNotIn(sensitive, rendered)
        self.assertIn("allowed_user_count=1", rendered)

    def test_scheduler_requires_fingerprint_pepper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            disabled = Settings.load(environ={"SCHEDULER_ENABLED": "true"}, base_dir=directory)
            enabled = Settings.load(
                environ={
                    "SCHEDULER_ENABLED": "true",
                    "ACCOUNT_BOT_FINGERPRINT_PEPPER": "test-pepper-long-value",
                },
                base_dir=directory,
            )
        self.assertFalse(disabled.scheduler_enabled)
        self.assertTrue(enabled.scheduler_enabled)

    def test_fingerprint_pepper_rejects_weak_or_reused_secrets(self) -> None:
        cases = (
            {"ACCOUNT_BOT_FINGERPRINT_PEPPER": "short"},
            {
                "ACCOUNT_BOT_TELEGRAM_TOKEN": "same-secret-value",
                "ACCOUNT_BOT_FINGERPRINT_PEPPER": "same-secret-value",
            },
        )
        for environment in cases:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(SettingsError):
                    Settings.load(environ=environment, base_dir=directory)

    def test_raw_account_identifier_environment_is_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(
                environ={
                    "KIWOOM_ACCOUNT_ID": "raw-kiwoom-account",
                    "TOSS_ACCOUNT_ID": "raw-toss-account",
                },
                base_dir=directory,
            )
        self.assertFalse(hasattr(settings, "kiwoom_account_id"))
        self.assertFalse(hasattr(settings, "toss_account_id"))
        self.assertNotIn("raw-kiwoom-account", repr(settings))
        self.assertNotIn("raw-toss-account", repr(settings))

    def test_allowlist_rejects_invalid_or_ambiguous_items(self) -> None:
        for value in ("123,,456", "abc", "0", "-1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(SettingsError):
                    Settings.load(
                        environ={"ALLOWED_TELEGRAM_USER_IDS": value},
                        base_dir=directory,
                    )

    def test_score_weights_must_be_bounded_and_sum_to_one(self) -> None:
        invalid_environments = (
            {"COMPANY_WEIGHT_FUNDAMENTAL": "0.36"},
            {"FINAL_WEIGHT_COMPANY": "1.1", "FINAL_WEIGHT_PORTFOLIO": "-0.1"},
            {"FINAL_WEIGHT_COMPANY": "nan"},
        )
        for environment in invalid_environments:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(SettingsError):
                    Settings.load(environ=environment, base_dir=directory)

    def test_sync_or_credential_bearing_urls_are_rejected(self) -> None:
        environments = (
            {"DATABASE_URL": "sqlite:///unsafe.db"},
            {"DATABASE_URL": "postgresql+asyncpg://user:secret@example.test/account"},
            {"KIWOOM_API_BASE_URL": "http://api.example.test"},
            {"TOSS_API_BASE_URL": "https://user:password@example.test"},
            {"KIWOOM_API_BASE_URL": "https://collector.example"},
            {"TOSS_API_BASE_URL": "https://collector.example"},
            {"TOSS_API_BASE_URL": "https://openapi.tossinvest.com:invalid"},
        )
        for environment in environments:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(SettingsError):
                    Settings.load(environ=environment, base_dir=directory)

    def test_only_official_broker_origins_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(
                environ={
                    "KIWOOM_API_BASE_URL": "https://mockapi.kiwoom.com/",
                    "TOSS_API_BASE_URL": "https://openapi.tossinvest.com/",
                },
                base_dir=directory,
            )
        self.assertEqual(settings.kiwoom_api_base_url, "https://mockapi.kiwoom.com")
        self.assertEqual(settings.toss_api_base_url, "https://openapi.tossinvest.com")

    def test_partial_broker_credentials_are_rejected_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SettingsError) as caught:
                Settings.load(
                    environ={"TOSS_CLIENT_SECRET": "must-never-appear"},
                    base_dir=directory,
                )
        self.assertNotIn("must-never-appear", str(caught.exception))

    def test_dotenv_parser_does_not_interpolate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "export FIRST=alpha # comment\nSECOND='${FIRST}'\n",
                encoding="utf-8",
            )
            values = load_dotenv_file(path)
        self.assertEqual(values, {"FIRST": "alpha", "SECOND": "${FIRST}"})


if __name__ == "__main__":
    unittest.main()
