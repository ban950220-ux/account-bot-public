from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from account_bot.runtime_lock import AlreadyRunningError, runtime_is_running, single_instance


def _try_lock(path: str, result: multiprocessing.Queue[bool]) -> None:
    try:
        with single_instance(path):
            result.put(True)
    except AlreadyRunningError:
        result.put(False)


class SingleInstanceTests(unittest.TestCase):
    def test_symbolic_link_lock_path_is_rejected_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_bytes(b"untouched")
            link = Path(directory) / "account_bot.lock"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(RuntimeError):
                runtime_is_running(link)
            with self.assertRaises(ValueError):
                with single_instance(link):
                    pass
            self.assertEqual(target.read_bytes(), b"untouched")

    def test_second_process_is_rejected_and_lock_recovers_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "account_bot.lock"
            self.assertFalse(runtime_is_running(lock_path))
            self.assertFalse(lock_path.exists())
            context = multiprocessing.get_context("spawn")
            result = context.Queue()

            with single_instance(lock_path):
                self.assertTrue(runtime_is_running(lock_path))
                process = context.Process(target=_try_lock, args=(str(lock_path), result))
                process.start()
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(result.get(timeout=2))

            process = context.Process(target=_try_lock, args=(str(lock_path), result))
            process.start()
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(result.get(timeout=2))
            self.assertFalse(runtime_is_running(lock_path))


if __name__ == "__main__":
    unittest.main()
