#!/usr/bin/env bash
# Installation only: never starts Telegram or loads application configuration.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
# Cloud images may have a read-only HOME. Keep tool state inside the checkout.
export XDG_DATA_HOME="$root/.cloud-cache/data"
export XDG_CACHE_HOME="$root/.cloud-cache/cache"
export XDG_STATE_HOME="$root/.cloud-cache/state"
export PIP_CACHE_DIR="$root/.cloud-cache/pip"
mkdir -p "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME" "$PIP_CACHE_DIR"
python_bin="${PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
command -v node >/dev/null || { echo 'Node 22.12+ is required'; exit 1; }
command -v pnpm >/dev/null || { echo 'pnpm is required; install it in the cloud image'; exit 1; }
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(a<22 || (a===22 && b<12)) process.exit(1)'
if [[ ! -x .venv/bin/python ]]; then
  "$python_bin" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -e '.[market,dev]'
.venv/bin/python -m pip check
pnpm --dir web install --frozen-lockfile --store-dir "$root/.pnpm-store" \
  --fetch-retries=0 --fetch-timeout=15000
printf '%s\n' 'Cloud setup complete. Run: .venv/bin/python scripts/cloud/validate.py'
