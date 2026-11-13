from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from account_bot.logging_config import configure_logging, redact_log_text


class LogRedactionTests(unittest.TestCase):
    def test_credentials_and_account_numbers_are_removed(self) -> None:
        rendered = redact_log_text(
            "Bearer abc.def secret=top-secret account_number=12345678 error_type=TimeoutError"
        )

        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("12345678", rendered)
        self.assertEqual(rendered.count("[REDACTED]"), 3)
        self.assertIn("error_type=TimeoutError", rendered)

    def test_telegram_bot_url_token_is_removed(self) -> None:
        rendered = redact_log_text("POST https://api.telegram.org/bot123456:fixture-secret/getMe")

        self.assertNotIn("123456:fixture-secret", rendered)
        self.assertIn("/bot[REDACTED]/getMe", rendered)

    def test_verbose_mode_never_enables_dependency_payload_or_sql_logs(self) -> None:
        root = logging.getLogger()
        previous_handlers = tuple(root.handlers)
        previous_root_level = root.level
        names = ("telegram", "telegram.ext", "aiosqlite", "sqlalchemy", "sqlalchemy.engine")
        previous_levels = {name: logging.getLogger(name).level for name in names}
        previous_application_level = logging.getLogger("account_bot").level
        with tempfile.TemporaryDirectory() as directory:
            try:
                configure_logging(Path(directory), verbose=True)
                self.assertEqual(
                    logging.getLogger("account_bot").getEffectiveLevel(), logging.DEBUG
                )
                for name in names:
                    with self.subTest(logger=name):
                        self.assertGreaterEqual(
                            logging.getLogger(name).getEffectiveLevel(), logging.WARNING
                        )
            finally:
                for handler in tuple(root.handlers):
                    handler.close()
                    root.removeHandler(handler)
                for handler in previous_handlers:
                    root.addHandler(handler)
                root.setLevel(previous_root_level)
                logging.getLogger("account_bot").setLevel(previous_application_level)
                for name, level in previous_levels.items():
                    logging.getLogger(name).setLevel(level)


if __name__ == "__main__":
    unittest.main()
