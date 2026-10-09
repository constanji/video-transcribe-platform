#!/usr/bin/env bash
# One-command release for the plugin_parser stack.
#
#   ./release.sh                          # full release of the current tree
#   RELEASE_SERVICES=api ./release.sh     # rebuild api only, retag web/moss
#   VERSION=2026.10.09-1 ./release.sh     # explicit image tag
#   DRY_RUN=1 ./release.sh                # print the plan, change nothing
#   ./release.sh --rollback               # back to the previous tag
#   ./release.sh --rollback=<tag>         # back to a specific tag
#
# Why it works this way: the deployment host cannot reach Docker Hub, and
# neither side can rely on GitHub being reachable. So the source goes over
# SSH/SFTP, the images are built and stored ON the host, and the stack is
# addressed by an immutable tag. Only the SSH channel is required.
#
# Flow: version -> package -> upload -> build on host -> roll out ->
#       verify (verify.sh) -> roll back automatically on failure.
#
# Env knobs: PYTHON, VERSION, RELEASE_SERVICES, DRY_RUN, VERIFY_TIMEOUT,
#            REQUIRE_CLEAN, KEEP_RELEASES, PRUNE_IMAGES, APP_ROOT,
#            REMOTE_SERVER_DIR, REMOTE_SRC_DIR, REMOTE_RELEASE_DIR, ENV_NAME
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_TOOLS="$REPO_ROOT/deployment-assets/tools/remote.py"

APP_ROOT="${APP_ROOT:-/opt/plugin_parser}"
REMOTE_SERVER_DIR="${REMOTE_SERVER_DIR:-$APP_ROOT/server}"
REMOTE_SRC_DIR="${REMOTE_SRC_DIR:-$APP_ROOT/src}"
REMOTE_RELEASE_DIR="${REMOTE_RELEASE_DIR:-$APP_ROOT/releases}"
ENV_NAME="${ENV_NAME:-deploy.env}"

DRY_RUN="${DRY_RUN:-0}"
RELEASE_SERVICES="${RELEASE_SERVICES:-}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-300}"
REQUIRE_CLEAN="${REQUIRE_CLEAN:-0}"
PRUNE_IMAGES="${PRUNE_IMAGES:-0}"
ROLLBACK_TAG=""

die()  { echo "release: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
info() { echo "    $*"; }

usage() {
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --rollback)   ROLLBACK_TAG="previous"; shift ;;
    --rollback=*) ROLLBACK_TAG="${1#*=}"; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "unknown argument: $1 (try --help)" ;;
  esac
done

case "$RELEASE_SERVICES" in
  ""|api|web|moss) ;;
  *) die "RELEASE_SERVICES must be empty, api, web or moss (got '$RELEASE_SERVICES')" ;;
esac

# ---------------------------------------------------------------- connection
discover_python() {
  local c
  for c in "${PYTHON:-}" "$REPO_ROOT/.venv/Scripts/python.exe" "$REPO_ROOT/.venv/bin/python" python3 python; do
    [ -n "$c" ] || continue
    if "$c" -c 'import paramiko' >/dev/null 2>&1; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

if ! PY="$(discover_python)"; then
  die "no Python interpreter with paramiko found.
       Install it ('pip install paramiko') or point PYTHON at one, e.g.
       PYTHON=$REPO_ROOT/.venv/Scripts/python.exe ./release.sh"
fi
info "using python : $PY"

# ------------------------------------------------------------------ helpers
# Release assets live under a *POSIX* temp dir on purpose: Git Bash sets TMPDIR
# to a Windows path (C:\...\Temp), and GNU tar then reads the drive letter in
# `-C C:\...` as a remote host and fails with "Cannot connect to C:".
TMP_BASE="/tmp"
if [ ! -d "$TMP_BASE" ] || [ ! -w "$TMP_BASE" ]; then
  TMP_BASE="${TMPDIR:-/tmp}"
fi

# Native (Windows) programs need Windows paths; MSYS tools need POSIX ones.
# convert when cygpath is available, otherwise pass through.
winpath() {
  case "$1" in
    /*) if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi ;;
    *)  printf '%s' "$1" ;;
  esac
}

trim() { printf '%s' "$1" | tr -d '[:space:]'; }

PY_EXE="$(winpath "$PY")"
REMOTE_TOOLS_W="$(winpath "$REMOTE_TOOLS")"
info "python (win) : $PY_EXE"

# r_run streams the remote command and propagates its exit status.
# r_cap captures stdout only (for reading values); r_mut is a mutation that
# is skipped under DRY_RUN.
#
# MSYS_NO_PATHCONV is set around these calls only: Git Bash would otherwise
# rewrite the POSIX remote paths (/opt/...) into Windows paths before Python
# sees them. Giving up that conversion means every *local* path handed to
# Python must already be in Windows form - hence $PY_EXE / $REMOTE_TOOLS_W.
r_run() { env MSYS_NO_PATHCONV=1 "$PY_EXE" "$REMOTE_TOOLS_W" exec "$1"; }
r_cap() { env MSYS_NO_PATHCONV=1 "$PY_EXE" "$REMOTE_TOOLS_W" exec -q "$1"; }
r_put() { env MSYS_NO_PATHCONV=1 "$PY_EXE" "$REMOTE_TOOLS_W" put "$(winpath "$1")" "$2" --resume; }
r_mut() {
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] remote: $1"
    return 0
  fi
  r_run "$1"
}

# ------------------------------------------------------------------- version
step "Resolving release version"
git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 \
  || die "$REPO_ROOT is not a git repository; set VERSION=... to release anyway"

COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
COMMIT_SHORT="$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD)"
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
DIRTY=0
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  DIRTY=1
  info "warning   : working tree has uncommitted changes; packaging it as-is"
  if [ "$REQUIRE_CLEAN" = "1" ]; then
    die "REQUIRE_CLEAN=1 but the working tree is dirty (commit or stash first)"
  fi
fi

BASE="$COMMIT_SHORT"
if [ "$DIRTY" = "1" ]; then
  BASE="${BASE}-dirty"
fi
VERSION="${VERSION:-${BASE}-$(date -u +%Y%m%d-%H%M%S)}"
VERSION="$(printf '%s' "$VERSION" | tr -c 'A-Za-z0-9._-' '-')"

info "version   : $VERSION"
info "commit    : $COMMIT_SHORT (branch $BRANCH, dirty=$DIRTY)"
info "host      : $REMOTE_SERVER_DIR  (env: $ENV_NAME)"

step "Reading the currently deployed tag"
PREV_TAG="$(trim "$(r_cap "grep -E '^IMAGE_TAG=' '$REMOTE_SERVER_DIR/$ENV_NAME' 2>/dev/null | tail -1 | cut -d= -f2")")"
if [ -n "$PREV_TAG" ]; then
  info "current   : $PREV_TAG"
else
  info "current   : <none recorded in $ENV_NAME>"
fi

# ------------------------------------------------------------------ rollback
if [ -n "$ROLLBACK_TAG" ]; then
  TARGET="$ROLLBACK_TAG"
  if [ "$TARGET" = "previous" ]; then
    TARGET="$(trim "$(r_cap "cat '$REMOTE_SERVER_DIR/.previous_tag' 2>/dev/null")")"
  fi
  [ -n "$TARGET" ] || die "no rollback target found; pass --rollback=<image-tag>"

  step "Rolling back to $TARGET"
  info "was: ${PREV_TAG:-<none>}"
  for svc in api web moss; do
    present="$(trim "$(r_cap "docker image inspect 'video-platform-$svc:$TARGET' >/dev/null 2>&1 && echo yes || echo no")")"
    if [ "$present" != "yes" ]; then
      die "image video-platform-$svc:$TARGET is not on the host; cannot roll back to it"
    fi
  done

  r_mut "cd '$REMOTE_SERVER_DIR' && sed -i 's|^IMAGE_TAG=.*|IMAGE_TAG=$TARGET|' '$ENV_NAME' && printf '%s' '$PREV_TAG' > .previous_tag && docker compose --env-file '$ENV_NAME' up -d"

  step "Verifying $TARGET"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] remote: VERIFY_TIMEOUT=$VERIFY_TIMEOUT bash verify.sh"
    info "[dry-run] rollback not performed"
    exit 0
  fi
  r_run "cd '$REMOTE_SERVER_DIR' && VERIFY_TIMEOUT=$VERIFY_TIMEOUT bash verify.sh" \
    || die "rollback to $TARGET failed verification; manual attention required"
  step "Rollback complete: now running $TARGET"
  exit 0
fi

# ----------------------------------------------------------------- packaging
step "Packaging source"
WORK="$(mktemp -d "${TMP_BASE%/}/plugin_parser-release.XXXXXX")"
STAGE="$WORK/stage"
TARBALL="$WORK/plugin_parser-$VERSION.tar.gz"
mkdir -p "$STAGE"
trap 'rm -rf "$WORK"' EXIT

# Never ship local venvs, build caches, runtime data or the multi-GB models
# and image tarballs that live under deployment-assets.
EXCLUDES=(
  --exclude=./.git --exclude='./.git/*'
  --exclude=./.venv --exclude='./.venv/*'
  --exclude=./venv --exclude='./venv/*'
  --exclude='*/node_modules' --exclude=./node_modules
  --exclude='*/.next' --exclude=./.next
  --exclude='*.tsbuildinfo'
  --exclude=./storage --exclude='*/storage'
  --exclude=./deployment-assets/images --exclude=./deployment-assets/models
  --exclude=./.pytest_cache --exclude=./.mypy_cache --exclude=./.ruff_cache
  --exclude='*/__pycache__' --exclude='*.pyc'
  --exclude=./.env --exclude='*.db' --exclude='*.db-*'
)

tar -cf - -C "$REPO_ROOT" "${EXCLUDES[@]}" . | tar -xf - -C "$STAGE"

cat > "$STAGE/BUILD_INFO" <<EOF
version=$VERSION
commit=$COMMIT
commit_short=$COMMIT_SHORT
branch=$BRANCH
dirty=$DIRTY
released_at=$(date -u +%FT%TZ)
release_host=$(hostname)
EOF

tar -czf "$TARBALL" -C "$STAGE" .
BYTES="$(wc -c < "$TARBALL" | tr -d ' ')"
MB="$(awk -v b="$BYTES" 'BEGIN { printf "%.1f", b / 1048576 }')"
info "packed    : $(find "$STAGE" -type f | wc -l | tr -d ' ') files, ${MB} MB"

# ------------------------------------------------------------------ shipping
step "Shipping to $REMOTE_RELEASE_DIR"
if [ "$DRY_RUN" = "1" ]; then
  info "[dry-run] remote: mkdir -p '$REMOTE_RELEASE_DIR' '$REMOTE_SRC_DIR'"
  info "[dry-run] upload $TARBALL -> $REMOTE_RELEASE_DIR/$VERSION.tar.gz"
else
  r_run "mkdir -p '$REMOTE_RELEASE_DIR' '$REMOTE_SRC_DIR'"
  r_put "$TARBALL" "$REMOTE_RELEASE_DIR/$VERSION.tar.gz"
fi

step "Unpacking on the host"
r_mut "set -e; rm -rf '$REMOTE_SRC_DIR'; mkdir -p '$REMOTE_SRC_DIR'; tar xzf '$REMOTE_RELEASE_DIR/$VERSION.tar.gz' -C '$REMOTE_SRC_DIR'; test -f '$REMOTE_SRC_DIR/deploy/api.Dockerfile'; cat '$REMOTE_SRC_DIR/BUILD_INFO'"

# ------------------------------------------------------------------- build
step "Building images on the host (tag $VERSION)"
if [ -n "$RELEASE_SERVICES" ]; then
  info "scope     : only '$RELEASE_SERVICES'; the others are retagged from $PREV_TAG"
fi
r_mut "cd '$REMOTE_SERVER_DIR' && set -a && . ./$ENV_NAME && set +a && SRC='$REMOTE_SRC_DIR' IMAGE_TAG='$VERSION' bash build-on-server.sh $RELEASE_SERVICES"

if [ -n "$RELEASE_SERVICES" ] && [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$VERSION" ]; then
  step "Retagging services that were not rebuilt"
  for svc in api web moss; do
    if [ "$svc" = "$RELEASE_SERVICES" ]; then
      continue
    fi
    r_mut "docker image inspect 'video-platform-$svc:$VERSION' >/dev/null 2>&1 || docker tag 'video-platform-$svc:$PREV_TAG' 'video-platform-$svc:$VERSION'"
    info "$svc -> image reuses $PREV_TAG"
  done
fi

# ------------------------------------------------------------------ rollout
step "Rolling out (IMAGE_TAG=$VERSION)"
r_mut "cd '$REMOTE_SERVER_DIR' && cp -f '$ENV_NAME' '$ENV_NAME.bak' && sed -i 's|^IMAGE_TAG=.*|IMAGE_TAG=$VERSION|' '$ENV_NAME' && printf '%s' '$PREV_TAG' > .previous_tag && docker compose --env-file '$ENV_NAME' up -d"

step "Verifying"
if [ "$DRY_RUN" = "1" ]; then
  info "[dry-run] remote: VERIFY_TIMEOUT=$VERIFY_TIMEOUT bash verify.sh"
  verify_ok=1
else
  if r_run "cd '$REMOTE_SERVER_DIR' && VERIFY_TIMEOUT=$VERIFY_TIMEOUT bash verify.sh"; then
    verify_ok=1
  else
    verify_ok=0
  fi
fi

if [ "$verify_ok" != "1" ]; then
  echo >&2
  step "Verification failed - rolling back" >&2
  if [ -z "$PREV_TAG" ]; then
    echo "release: no previous tag recorded, leaving the stack as-is for inspection" >&2
    echo "release: inspect with: cd $REMOTE_SERVER_DIR && docker compose --env-file $ENV_NAME logs -f --tail=200 api" >&2
    exit 1
  fi
  echo "release: restoring IMAGE_TAG=$PREV_TAG" >&2
  if r_run "cd '$REMOTE_SERVER_DIR' && sed -i 's|^IMAGE_TAG=.*|IMAGE_TAG=$PREV_TAG|' '$ENV_NAME' && docker compose --env-file '$ENV_NAME' up -d && VERIFY_TIMEOUT=$VERIFY_TIMEOUT bash verify.sh"; then
    echo "release: rolled back to $PREV_TAG and it verifies again" >&2
  else
    echo "release: rollback to $PREV_TAG ALSO failed verification - manual attention required" >&2
  fi
  echo "release: release $VERSION was NOT promoted" >&2
  exit 1
fi

# ------------------------------------------------------------------ bookkeeping
if [ "$DRY_RUN" != "1" ]; then
  RELEASE_LINE="$(date -u +%FT%TZ) version=$VERSION commit=$COMMIT_SHORT dirty=$DIRTY previous=${PREV_TAG:-none}"
  r_run "printf '%s\n' '$RELEASE_LINE' >> '$REMOTE_SERVER_DIR/releases.log'"

  if [ "$KEEP_RELEASES" != "0" ]; then
    r_run "cd '$REMOTE_RELEASE_DIR' && ls -1t ./*.tar.gz 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | xargs -r rm -f"
  fi

  if [ "$PRUNE_IMAGES" = "1" ]; then
    KEEP_RE="$VERSION"
    if [ -n "$PREV_TAG" ]; then
      KEEP_RE="$VERSION|$PREV_TAG"
    fi
    step "Pruning superseded images (keeping current and previous tag)"
    info "note      : older tags can no longer be rolled back to"
    r_run "docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^video-platform-(api|web|moss):' | grep -Ev ':($KEEP_RE)\$' | xargs -r docker image rm -f"
  fi

  step "Deployed"
  if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$VERSION" ]; then
    info "previous  : $PREV_TAG  (roll back with: $0 --rollback)"
  fi
  r_run "cd '$REMOTE_SERVER_DIR' && docker compose --env-file '$ENV_NAME' ps"
else
  step "Dry run complete - nothing was changed"
  info "would build   : video-platform-{api,web,moss}:$VERSION on the host"
  info "would roll out: IMAGE_TAG=$VERSION (was ${PREV_TAG:-<none>})"
  info "would verify  : verify.sh, rolling back automatically on failure"
fi
