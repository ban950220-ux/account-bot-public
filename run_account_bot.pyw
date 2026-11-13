"""Windowless launcher used by the Windows logon task."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from account_bot.cli import main  # noqa: E402

raise SystemExit(main(["--env-file", str(PROJECT_ROOT / ".env"), "run"]))
