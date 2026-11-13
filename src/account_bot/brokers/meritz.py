"""Meritz adapter using an explicit normalized export when provided.

No public personal-account REST API specification was available at build time,
so this module deliberately performs no scraping, reverse engineering or RPA.
"""

from __future__ import annotations

from pathlib import Path

from .snapshot import JsonSnapshotBroker


class MeritzBroker(JsonSnapshotBroker):
    def __init__(self, snapshot_path: Path | None = None) -> None:
        super().__init__("meritz", snapshot_path)
