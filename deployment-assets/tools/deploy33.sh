#!/usr/bin/env bash
# deploy33.sh - one-command release to the 10.0.0.33 GPU host.
#
# Sources .remote.33.env (credentials, gitignored) for HOST/USER/PASSWORD/
# APP_ROOT, then runs the standard release.sh pipeline (package -> upload ->
# build on server -> roll out -> verify, with automatic rollback on failure).
#
# Usage (via the project-root deploy33.cmd, or directly from Git Bash):
#   ./deploy33.sh                 # full release (api + web + moss)
#   ./deploy33.sh --dry-run       # package + show what would happen
#   ./deploy33.sh --rollback      # roll back to the previous image tag
#
# Optional overrides (set before calling):
#   RELEASE_SERVICES=api|web|moss   rebuild only that service
#   REQUIRE_CLEAN=1                 fail if the working tree is dirty
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/.remote.33.env"
SERVER_DIR="$(cd "$HERE/../server" && pwd)"

if [ ! -f "$ENV_FILE" ]; then
  echo "deploy33: missing $ENV_FILE" >&2
  echo "  create it with: HOST=... USER=jyj KEY=/c/Users/金/.ssh/id_ed25519 APP_ROOT=/home/goodeep/jzj" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

# Defaults (overridable from the caller's environment).
export APP_ROOT="${APP_ROOT:-/home/goodeep/jzj}"
export R_TIMEOUT="${R_TIMEOUT:-7200}"
export VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-600}"

exec "$SERVER_DIR/release.sh" "$@"
