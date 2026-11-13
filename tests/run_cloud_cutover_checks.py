"""Credential-free focused runner using project dependencies; not full pytest/Ruff."""
import runpy
import socket
import sys
import unittest
from pathlib import Path


def deny(*args, **kwargs):
    raise RuntimeError("network disabled for synthetic validation")


def loopback_only(connect):
    # Windows asyncio connects its internal socketpair over 127.0.0.1.
    def guarded(self, address, *args, **kwargs):
        if isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1"):
            return connect(self, address, *args, **kwargs)
        return deny()

    return guarded


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root / "tests"))
    socket.socket.connect = loopback_only(socket.socket.connect)
    socket.socket.connect_ex = loopback_only(socket.socket.connect_ex)
    socket.create_connection = deny
    suite = unittest.TestSuite()
    for pattern in ("test_recurring_allocation.py", "test_reports.py", "test_runtime_lock.py",
                    "test_cloud_safety_regressions.py", "test_cloud_projection.py"):
        suite.addTests(unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern))
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        raise SystemExit(1)
    tests = runpy.run_path(str(root / "tests/test_cloud_js_parity.py"))
    count = 0
    for name, function in tests.items():
        if name.startswith("test_"):
            function()
            count += 1
            print("PASS direct parity:", name)
    print("PASS", suite.countTestCases(), "unittest and", count, "direct parity functions")


if __name__ == "__main__":
    main()
