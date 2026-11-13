"""Application logging with a final defensive redaction boundary."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTIONS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(token|secret|password|app[_-]?key|client[_-]?secret|refresh[_-]?token)\b\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)\b(account[_-]?(?:number|no))\b\s*[:=]\s*[^\s,;]+"),
)
_TELEGRAM_BOT_URL = re.compile(r"(?i)(/bot)[A-Za-z0-9:_-]+(?=/|\b)")


def redact_log_text(value: str) -> str:
    result = _TELEGRAM_BOT_URL.sub(r"\1[REDACTED]", value)
    for pattern in _REDACTIONS:
        result = pattern.sub(
            lambda match: (
                f"{match.group(1)}=[REDACTED]" if match.lastindex else "Bearer [REDACTED]"
            ),
            result,
        )
    return result


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_text(record.getMessage())
        record.args = ()
        return True


def configure_logging(log_dir: Path, *, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "account_bot.log"
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    redaction = SecretRedactionFilter()

    root = logging.getLogger()
    # Verbose mode is for this application, never for dependency wire/SQL
    # logging. PTB debug records can contain the Bot API URL (and therefore the
    # full token) plus sent report bodies; aiosqlite records bound values.
    root.setLevel(logging.INFO)
    logging.getLogger("account_bot").setLevel(level)
    for existing in tuple(root.handlers):
        root.removeHandler(existing)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(redaction)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    root.addHandler(file_handler)

    # Avoid third-party wire, payload, and bound-parameter logs even in verbose
    # application mode. The URL redaction above remains a defense in depth.
    for logger_name in (
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
        "aiosqlite",
        "sqlalchemy",
        "sqlalchemy.engine",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    return log_path
