"""Credential-free cloud validation; never call the Telegram run entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]


def clean_environment(directory: Path) -> dict[str, str]:
    # Do not inherit credentials, database URLs, private fixture paths or pytest hooks.
    # Windows cannot initialise Winsock or locate system DLLs without these
    # OS-location variables; they carry no credentials or project data.
    system = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
              if name in os.environ}
    return {
        **system,
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(directory),
        "TMPDIR": str(directory),
        "LANG": "C.UTF-8",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "ACCOUNT_BOT_ENV_FILE": str(directory / "absent.env"),
        "ACCOUNT_BOT_WEB_DATABASE_URL": f"sqlite+aiosqlite:///{directory / 'smoke.db'}",
        "ACCOUNT_BOT_WEB_LIVE_MARKET_DATA": "false",
    }


def deny_connect(*args, **kwargs):
    raise RuntimeError("Outbound network is disabled in cloud validation")


def loopback_only(connect):
    # Windows asyncio builds its internal wake-up socketpair by connecting to
    # 127.0.0.1. That never leaves the host; every other destination stays denied.
    def guarded(self, address, *args, **kwargs):
        if isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1"):
            return connect(self, address, *args, **kwargs)
        return deny_connect()

    return guarded


def offline_worker(mode: str) -> int:
    # Defence in depth for Python transports. Tests use mocked providers; this is
    # not an OS sandbox for arbitrary native libraries or child processes.
    socket.socket.connect = loopback_only(socket.socket.connect)
    socket.socket.connect_ex = loopback_only(socket.socket.connect_ex)
    socket.create_connection = deny_connect
    if mode == "_tests":
        import pytest

        return pytest.main(["-p", "pytest_asyncio.plugin", "-q", "tests"])
    import uvicorn

    from account_bot.web_api import create_app

    # POSIX inherits the reserved listening socket; Windows cannot pass file
    # descriptors to a child, so it receives the reserved loopback port instead.
    listen = ({"fd": int(os.environ["ACCOUNT_BOT_CLOUD_SOCKET_FD"])}
              if "ACCOUNT_BOT_CLOUD_SOCKET_FD" in os.environ else
              {"host": "127.0.0.1", "port": int(os.environ["ACCOUNT_BOT_CLOUD_PORT"])})
    uvicorn.run(
        create_app(database_url=os.environ["ACCOUNT_BOT_WEB_DATABASE_URL"]),
        loop="asyncio",
        **listen,
    )
    return 0


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def smoke(directory: Path, env: dict[str, str]) -> None:
    # Pass a reserved loopback socket to avoid fixed-port conflicts and races.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        if os.name == "nt":
            port = probe.getsockname()[1]
            probe.close()
            smoke_server(directory, {**env, "ACCOUNT_BOT_CLOUD_PORT": str(port)}, port, ())
            return
        probe.listen()
        smoke_server(directory, {**env, "ACCOUNT_BOT_CLOUD_SOCKET_FD": str(probe.fileno())},
                     probe.getsockname()[1], (probe.fileno(),))


def smoke_server(directory: Path, env: dict[str, str], port: int,
                 inherited: tuple[int, ...]) -> None:
    with (directory / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_serve"],
            cwd=directory, env=env, stdout=log, stderr=log,
            **({"pass_fds": inherited} if inherited else {}),
        )
        try:
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Offline web startup failed")
                try:
                    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                        import json

                        assert json.load(response) == {"status": "ok", "brokerPolicy": "READ_ONLY"}
                    print("PASS: real offline web startup and HTTP health (temporary DB)")
                    return
                except (URLError, TimeoutError):
                    time.sleep(0.2)
            raise RuntimeError("Offline startup timed out after 30 seconds")
        finally:
            if os.name == "nt":
                # A venv python.exe is a launcher; terminate() alone would leave the
                # real interpreter running with the temporary database still open.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, check=False)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            # Windows has no SIGTERM: terminate() ends the child with exit code 1.
            if process.returncode not in ((0, 1) if os.name == "nt" else (0, -15)):
                log.seek(0)
                print(log.read())  # Only a clean-env fixture process writes this log.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "smoke", "_tests", "_serve"], default="check", nargs="?")
    mode = parser.parse_args().mode
    if mode.startswith("_"):
        return offline_worker(mode)
    with tempfile.TemporaryDirectory(prefix="account-bot-cloud-") as temporary:
        directory = Path(temporary)
        env = clean_environment(directory)
        if mode == "check":
            # Tests anchored to PROJECT_ROOT cannot accidentally read the checkout's
            # .env or runtime data. Only source and public test fixtures are copied.
            for name in ("src", "tests", "examples"):
                shutil.copytree(ROOT / name, directory / name,
                                ignore=shutil.ignore_patterns("__pycache__", ".env", ".env.*"))
            # Cloud parity tests import these checked-in source files by path.
            # Copy only source/schema, never operating data or local deployment state.
            (directory / "scripts" / "cloud").mkdir(parents=True)
            for name in ("migration_preflight.py", "migration_projection.py",
                         "rollback_reconcile.py", "verify_transfer.py"):
                shutil.copy2(ROOT / "scripts/cloud" / name,
                             directory / "scripts/cloud" / name)
            shutil.copytree(ROOT / "deploy/cloudflare-js", directory / "deploy/cloudflare-js")
            (directory / "deploy/cloudflare").mkdir(parents=True)
            shutil.copy2(ROOT / "deploy/cloudflare/schema.sql",
                         directory / "deploy/cloudflare/schema.sql")
            shutil.copy2(ROOT / "pyproject.toml", directory / "pyproject.toml")
            run([sys.executable, "-m", "compileall", "-q", "src", "tests"], cwd=directory, env=env)
            run([sys.executable, str(Path(__file__).resolve()), "_tests"], cwd=directory, env=env)
            run([sys.executable, "-m", "ruff", "check", "."], cwd=ROOT, env=env)
            run([sys.executable, "-m", "pip", "check"], cwd=directory, env=env)
        smoke(directory, env)
        if mode == "check":
            # Invoke installed tools directly: newer pnpm versions may implicitly
            # download missing node_modules even for `pnpm run build`.
            web = ROOT / "web"
            tsc = web / "node_modules/typescript/bin/tsc"
            vite = web / "node_modules/vite/bin/vite.js"
            if not tsc.is_file() or not vite.is_file():
                raise SystemExit("Frontend dependencies missing: rerun cloud/setup.sh with registry access")
            run(["node", str(tsc), "-b"], cwd=web, env=env)
            run(["node", str(vite), "build", "--configLoader", "runner"], cwd=web, env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
