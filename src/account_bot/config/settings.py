"""Dependency-light, fail-closed application settings.

The module intentionally does not use ``pydantic-settings`` or mutate
``os.environ``. Values from the process environment override values read from
``.env``. Secret and personally identifying values are excluded from reprs.
"""

from __future__ import annotations

import ast
import math
import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SettingsError(ValueError):
    """Raised when application configuration is malformed or unsafe."""


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASYNC_DATABASE_SCHEMES = frozenset({"sqlite+aiosqlite"})


def _strip_unquoted_comment(value: str) -> str:
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _decode_env_value(value: str, *, path: Path, line_number: int) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] not in {"'", '"'}:
        return _strip_unquoted_comment(value)
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise SettingsError(f"{path}:{line_number}: unterminated quoted value")
    inner = value[1:-1]
    if quote == "'":
        return inner
    try:
        decoded = ast.literal_eval(value)
        if not isinstance(decoded, str):
            raise ValueError
        return decoded
    except (SyntaxError, ValueError) as exc:
        raise SettingsError(f"{path}:{line_number}: invalid quoted value") from exc


def load_dotenv_file(path: str | Path, *, required: bool = False) -> dict[str, str]:
    """Read a small, deterministic subset of dotenv syntax without logging values.

    Supported syntax is ``KEY=value``, optional ``export`` prefixes, comments,
    and single/double quoted values. Variable interpolation is deliberately not
    supported so a secret cannot be expanded unexpectedly.
    """

    env_path = Path(path)
    if not env_path.exists():
        if required:
            raise SettingsError(f"environment file does not exist: {env_path}")
        return {}
    if not env_path.is_file():
        raise SettingsError(f"environment path is not a file: {env_path}")

    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise SettingsError(f"unable to read environment file: {env_path}") from exc

    for line_number, original_line in enumerate(lines, start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SettingsError(f"{env_path}:{line_number}: expected KEY=value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            raise SettingsError(f"{env_path}:{line_number}: invalid environment key")
        values[key] = _decode_env_value(raw_value, path=env_path, line_number=line_number)
    return values


def _optional_text(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key, "").strip()
    return value or None


def _parse_bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{key} must be a boolean")


def _parse_int(
    values: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = values.get(key)
    try:
        parsed = default if raw is None or not raw.strip() else int(raw.strip())
    except ValueError as exc:
        raise SettingsError(f"{key} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise SettingsError(f"{key} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise SettingsError(f"{key} must be at most {maximum}")
    return parsed


def _parse_float(values: Mapping[str, str], key: str, default: float) -> float:
    raw = values.get(key)
    try:
        parsed = default if raw is None or not raw.strip() else float(raw.strip())
    except ValueError as exc:
        raise SettingsError(f"{key} must be numeric") from exc
    if not math.isfinite(parsed):
        raise SettingsError(f"{key} must be finite")
    return parsed


def _parse_time(values: Mapping[str, str], key: str, default: str) -> time:
    raw = values.get(key, default).strip() or default
    try:
        parts = raw.split(":")
        if len(parts) not in {2, 3}:
            raise ValueError
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise SettingsError(f"{key} must use HH:MM or HH:MM:SS") from exc


def _parse_allowlist(raw: str | None) -> frozenset[int]:
    if raw is None or not raw.strip():
        return frozenset()
    result: set[int] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            raise SettingsError("ALLOWED_TELEGRAM_USER_IDS contains an empty item")
        try:
            user_id = int(candidate)
        except ValueError as exc:
            raise SettingsError("ALLOWED_TELEGRAM_USER_IDS must contain only integer IDs") from exc
        if user_id <= 0:
            raise SettingsError("ALLOWED_TELEGRAM_USER_IDS must contain only positive IDs")
        result.add(user_id)
    return frozenset(result)


def _resolve_path(raw: str | None, *, base_dir: Path, default: str) -> Path:
    candidate = Path((raw or default).strip()).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def _sqlite_url_from_path(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _normalize_database_url(raw: str, *, base_dir: Path) -> str:
    value = raw.strip()
    if not value or "://" not in value:
        raise SettingsError("DATABASE_URL must be a complete async SQLAlchemy URL")
    scheme = value.split(":", 1)[0].lower()
    if scheme not in _ASYNC_DATABASE_SCHEMES:
        allowed = ", ".join(sorted(_ASYNC_DATABASE_SCHEMES))
        raise SettingsError(f"DATABASE_URL must use one of: {allowed}")

    if scheme == "sqlite+aiosqlite":
        prefix = "sqlite+aiosqlite:///"
        if not value.startswith(prefix):
            raise SettingsError("SQLite DATABASE_URL must use sqlite+aiosqlite:///")
        database_part, separator, query = value[len(prefix) :].partition("?")
        if database_part in {":memory:", "file::memory:"} or database_part.startswith("file:"):
            return value
        if not database_part:
            raise SettingsError("SQLite DATABASE_URL must include a database path")
        path = Path(database_part).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve(strict=False)
        normalized = _sqlite_url_from_path(path)
        return f"{normalized}?{query}" if separator else normalized

    raise SettingsError("only sqlite+aiosqlite is supported by the MVP repository")


def _validate_broker_origin(
    value: str | None,
    key: str,
    *,
    allowed_hosts: frozenset[str],
) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SettingsError(f"{key} must be an HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SettingsError(f"{key} has an invalid port") from exc
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or parsed.path.rstrip("/")
        or parsed.hostname.lower() not in allowed_hosts
    ):
        hosts = ", ".join(sorted(allowed_hosts))
        raise SettingsError(f"{key} must use an official broker origin: {hosts}")
    return f"https://{parsed.hostname.lower()}"


def _validate_pair(name: str, first: str | None, second: str | None) -> None:
    if bool(first) != bool(second):
        raise SettingsError(f"{name} credentials must be configured together")


def _validate_weights(name: str, values: tuple[float, ...]) -> None:
    if any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in values):
        raise SettingsError(f"{name} weights must each be between 0 and 1")
    if not math.isclose(math.fsum(values), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise SettingsError(f"{name} weights must total 1.0")


@dataclass(frozen=True, slots=True)
class CompanyScoreWeights:
    fundamental: float = 0.35
    valuation: float = 0.25
    growth: float = 0.20
    moat: float = 0.10
    risk: float = 0.10

    def __post_init__(self) -> None:
        _validate_weights(
            "company score",
            (self.fundamental, self.valuation, self.growth, self.moat, self.risk),
        )


@dataclass(frozen=True, slots=True)
class FinalScoreWeights:
    company: float = 0.70
    portfolio: float = 0.30

    def __post_init__(self) -> None:
        _validate_weights("final score", (self.company, self.portfolio))


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    """Validated settings with a deny-by-default Telegram allowlist."""

    base_dir: Path
    data_dir: Path
    log_dir: Path
    report_dir: Path
    cache_dir: Path
    database_url: str
    sqlite_busy_timeout_ms: int
    timezone_name: str
    scheduler_enabled: bool
    daily_report_time: time
    weekly_report_time: time
    weekly_report_weekday: int
    company_weights: CompanyScoreWeights
    final_weights: FinalScoreWeights
    telegram_token: str | None = field(default=None, repr=False)
    fingerprint_pepper: str | None = field(default=None, repr=False)
    allowed_telegram_user_ids: frozenset[int] = field(default_factory=frozenset, repr=False)
    kiwoom_main_app_key: str | None = field(default=None, repr=False)
    kiwoom_main_app_secret: str | None = field(default=None, repr=False)
    kiwoom_accum_app_key: str | None = field(default=None, repr=False)
    kiwoom_accum_app_secret: str | None = field(default=None, repr=False)
    toss_client_id: str | None = field(default=None, repr=False)
    toss_client_secret: str | None = field(default=None, repr=False)
    kiwoom_api_base_url: str | None = None
    toss_api_base_url: str | None = None
    kiwoom_snapshot_path: Path | None = field(default=None, repr=False)
    toss_snapshot_path: Path | None = field(default=None, repr=False)
    meritz_snapshot_path: Path | None = field(default=None, repr=False)
    meritz_accumul_snapshot_path: Path | None = field(default=None, repr=False)

    @classmethod
    def load(
        cls,
        *,
        env_file: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        base_dir: str | Path | None = None,
    ) -> "Settings":
        process_values = dict(os.environ if environ is None else environ)
        explicit_file = env_file is not None
        file_candidate = env_file or process_values.get("ACCOUNT_BOT_ENV_FILE") or ".env"
        file_path = Path(file_candidate).expanduser()
        if base_dir is None and explicit_file:
            resolved_base = file_path.resolve(strict=False).parent
        else:
            resolved_base = Path(base_dir or Path.cwd()).expanduser().resolve(strict=False)
        if not file_path.is_absolute():
            file_path = resolved_base / file_path
        file_values = load_dotenv_file(file_path, required=explicit_file)
        values = {**file_values, **process_values}

        data_dir = _resolve_path(
            values.get("ACCOUNT_BOT_DATA_DIR"),
            base_dir=resolved_base,
            default="data",
        )
        log_dir = _resolve_path(
            values.get("ACCOUNT_BOT_LOG_DIR"),
            base_dir=resolved_base,
            default="logs",
        )
        report_dir = _resolve_path(
            values.get("ACCOUNT_BOT_REPORT_DIR"),
            base_dir=resolved_base,
            default="reports",
        )
        cache_dir = _resolve_path(
            values.get("ACCOUNT_BOT_CACHE_DIR"),
            base_dir=resolved_base,
            default="cache",
        )
        raw_database_url = values.get("DATABASE_URL")
        database_url = _normalize_database_url(
            raw_database_url or _sqlite_url_from_path(data_dir / "account_bot.db"),
            base_dir=resolved_base,
        )

        timezone_name = values.get("ACCOUNT_BOT_TIMEZONE", "Asia/Seoul").strip()
        if not timezone_name:
            raise SettingsError("ACCOUNT_BOT_TIMEZONE must not be empty")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise SettingsError("ACCOUNT_BOT_TIMEZONE is not available") from exc

        telegram_token = _optional_text(values, "ACCOUNT_BOT_TELEGRAM_TOKEN")
        fingerprint_pepper = _optional_text(values, "ACCOUNT_BOT_FINGERPRINT_PEPPER")
        # The two Kiwoom credentials are independent logical account sources.
        # Legacy single-account names remain accepted as the MAIN source.
        kiwoom_main_key = _optional_text(values, "KIWOOM_MAIN_APPKEY") or _optional_text(
            values, "KIWOOM_APP_KEY"
        )
        kiwoom_main_secret = _optional_text(
            values, "KIWOOM_MAIN_SECRET"
        ) or _optional_text(values, "KIWOOM_APP_SECRET")
        kiwoom_accum_key = _optional_text(values, "KIWOOM_ACCUM_APPKEY")
        kiwoom_accum_secret = _optional_text(values, "KIWOOM_ACCUM_SECRET")
        toss_client_id = _optional_text(values, "TOSS_CLIENT_ID")
        toss_client_secret = _optional_text(values, "TOSS_CLIENT_SECRET")
        _validate_pair("KIWOOM_MAIN", kiwoom_main_key, kiwoom_main_secret)
        _validate_pair("KIWOOM_ACCUM", kiwoom_accum_key, kiwoom_accum_secret)
        _validate_pair("TOSS", toss_client_id, toss_client_secret)
        if fingerprint_pepper is not None:
            if len(fingerprint_pepper) < 16:
                raise SettingsError("ACCOUNT_BOT_FINGERPRINT_PEPPER must be at least 16 characters")
            if fingerprint_pepper in {
                telegram_token,
                kiwoom_main_key,
                kiwoom_main_secret,
                kiwoom_accum_key,
                kiwoom_accum_secret,
                toss_client_id,
                toss_client_secret,
            }:
                raise SettingsError(
                    "ACCOUNT_BOT_FINGERPRINT_PEPPER must not reuse another credential"
                )

        company_weights = CompanyScoreWeights(
            fundamental=_parse_float(values, "COMPANY_WEIGHT_FUNDAMENTAL", 0.35),
            valuation=_parse_float(values, "COMPANY_WEIGHT_VALUATION", 0.25),
            growth=_parse_float(values, "COMPANY_WEIGHT_GROWTH", 0.20),
            moat=_parse_float(values, "COMPANY_WEIGHT_MOAT", 0.10),
            risk=_parse_float(values, "COMPANY_WEIGHT_RISK", 0.10),
        )
        final_weights = FinalScoreWeights(
            company=_parse_float(values, "FINAL_WEIGHT_COMPANY", 0.70),
            portfolio=_parse_float(values, "FINAL_WEIGHT_PORTFOLIO", 0.30),
        )

        def optional_snapshot_path(key: str) -> Path | None:
            raw = _optional_text(values, key)
            if raw is None:
                return None
            return _resolve_path(raw, base_dir=resolved_base, default=raw)

        return cls(
            base_dir=resolved_base,
            data_dir=data_dir,
            log_dir=log_dir,
            report_dir=report_dir,
            cache_dir=cache_dir,
            database_url=database_url,
            sqlite_busy_timeout_ms=_parse_int(
                values,
                "SQLITE_BUSY_TIMEOUT_MS",
                5_000,
                minimum=1,
                maximum=120_000,
            ),
            timezone_name=timezone_name,
            scheduler_enabled=(
                _parse_bool(values, "SCHEDULER_ENABLED", True) and bool(fingerprint_pepper)
            ),
            daily_report_time=_parse_time(values, "DAILY_REPORT_TIME", "08:00"),
            weekly_report_time=_parse_time(values, "WEEKLY_REPORT_TIME", "09:00"),
            weekly_report_weekday=_parse_int(
                values,
                "WEEKLY_REPORT_WEEKDAY",
                5,
                minimum=0,
                maximum=6,
            ),
            company_weights=company_weights,
            final_weights=final_weights,
            telegram_token=telegram_token,
            fingerprint_pepper=fingerprint_pepper,
            allowed_telegram_user_ids=_parse_allowlist(values.get("ALLOWED_TELEGRAM_USER_IDS")),
            kiwoom_main_app_key=kiwoom_main_key,
            kiwoom_main_app_secret=kiwoom_main_secret,
            kiwoom_accum_app_key=kiwoom_accum_key,
            kiwoom_accum_app_secret=kiwoom_accum_secret,
            toss_client_id=toss_client_id,
            toss_client_secret=toss_client_secret,
            kiwoom_api_base_url=_validate_broker_origin(
                _optional_text(values, "KIWOOM_API_BASE_URL"),
                "KIWOOM_API_BASE_URL",
                allowed_hosts=frozenset({"api.kiwoom.com", "mockapi.kiwoom.com"}),
            ),
            toss_api_base_url=_validate_broker_origin(
                _optional_text(values, "TOSS_API_BASE_URL"),
                "TOSS_API_BASE_URL",
                allowed_hosts=frozenset({"openapi.tossinvest.com"}),
            ),
            kiwoom_snapshot_path=optional_snapshot_path("KIWOOM_SNAPSHOT_PATH"),
            toss_snapshot_path=optional_snapshot_path("TOSS_SNAPSHOT_PATH"),
            meritz_snapshot_path=optional_snapshot_path("MERITZ_SNAPSHOT_PATH"),
            meritz_accumul_snapshot_path=optional_snapshot_path(
                "MERITZ_ACCUMUL_SNAPSHOT_PATH"
            ),
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def kiwoom_app_key(self) -> str | None:
        """Backward-compatible alias for the MAIN Kiwoom credential."""

        return self.kiwoom_main_app_key

    @property
    def kiwoom_app_secret(self) -> str | None:
        """Backward-compatible alias for the MAIN Kiwoom credential."""

        return self.kiwoom_main_app_secret

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.allowed_telegram_user_ids)

    def is_telegram_user_allowed(self, user_id: int | None) -> bool:
        """Return False for missing/empty/malformed input and empty configuration."""

        return (
            isinstance(user_id, int)
            and not isinstance(user_id, bool)
            and user_id > 0
            and user_id in self.allowed_telegram_user_ids
        )

    def credential_status(self) -> Mapping[str, bool]:
        """Return booleans only, never credential values."""

        return MappingProxyType(
            {
                "telegram": bool(self.telegram_token),
                "kiwoom_main": bool(
                    self.kiwoom_main_app_key and self.kiwoom_main_app_secret
                ),
                "kiwoom_accum": bool(
                    self.kiwoom_accum_app_key and self.kiwoom_accum_app_secret
                ),
                "kiwoom": bool(
                    self.kiwoom_main_app_key and self.kiwoom_main_app_secret
                ),
                "toss": bool(self.toss_client_id and self.toss_client_secret),
                "meritz": False,
            }
        )

    def ensure_runtime_directories(self) -> None:
        for path in (self.data_dir, self.log_dir, self.report_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        backend = self.database_url.split(":", 1)[0]
        status = self.credential_status()
        return (
            "Settings("
            f"database_backend={backend!r}, timezone_name={self.timezone_name!r}, "
            f"scheduler_enabled={self.scheduler_enabled!r}, "
            f"allowed_user_count={len(self.allowed_telegram_user_ids)}, "
            f"telegram_configured={status['telegram']}, "
            f"kiwoom_main_configured={status['kiwoom_main']}, "
            f"kiwoom_accum_configured={status['kiwoom_accum']}, "
            f"toss_configured={status['toss']}"
            ")"
        )
