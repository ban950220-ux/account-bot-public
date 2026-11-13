"""Cross-process single-instance guard for the long-running bot runtime."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class AlreadyRunningError(RuntimeError):
    """Raised when another process already owns the runtime lock."""


def _lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def runtime_is_running(lock_path: str | Path) -> bool:
    """Probe an existing lock without creating or modifying its file."""
    if Path(lock_path).is_symlink():
        raise RuntimeError("runtime lock path must not be a symbolic link")
    try:
        with Path(lock_path).open("r+b") as handle:
            try:
                _lock(handle)
            except OSError:
                return True
            _unlock(handle)
            return False
    except FileNotFoundError:
        return False


@contextmanager
def single_instance(lock_path: str | Path) -> Iterator[None]:
    """Hold an OS-backed file lock until the bot runtime exits.

    The small lock file is intentionally retained after shutdown.  Ownership is
    represented by the operating-system lock, so an unclean exit cannot leave a
    stale PID marker that prevents the next legitimate start.
    """

    path = Path(lock_path)
    if path.is_symlink():
        raise ValueError("runtime lock path must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            _lock(handle)
        except OSError as exc:
            raise AlreadyRunningError("another account-bot runtime is already running") from exc
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()
