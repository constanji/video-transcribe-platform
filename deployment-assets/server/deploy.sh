#!/usr/bin/env bash
set -euo pipefail

SERVER_HOST="${SERVER_HOST:?Set SERVER_HOST, for example root@8.136.26.124}"
SERVER_PORT="${SERVER_PORT:-22}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_ASSETS="${REMOTE_ASSETS:-/opt/plugin_parser}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" -p "$SERVER_PORT")
RSYNC=(rsync -az --delete -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i $SSH_KEY -p $SERVER_PORT")

USE_RSYNC=false
if command -v rsync >/dev/null 2>&1 && "${SSH[@]}" "$SERVER_HOST" "command -v rsync >/dev/null 2>&1"; then
  USE_RSYNC=true
fi

echo "==> Preparing remote directories"
"${SSH[@]}" "$SERVER_HOST" "mkdir -p '$REMOTE_ASSETS/server' '$REMOTE_ASSETS/images' '$REMOTE_ASSETS/models'"

echo "==> Syncing deployment descriptors"
if "$USE_RSYNC"; then
  "${RSYNC[@]}" "$REPO_ROOT/deployment-assets/server/" "$SERVER_HOST:$REMOTE_ASSETS/server/"
else
  COPYFILE_DISABLE=1 tar -C "$REPO_ROOT/deployment-assets/server" -czf - . | "${SSH[@]}" "$SERVER_HOST" "tar --warning=no-unknown-keyword -C '$REMOTE_ASSETS/server' -xzf -"
fi

echo "==> Starting services"
"${SSH[@]}" "$SERVER_HOST" "cd '$REMOTE_ASSETS/server' && test -f deploy.env || { echo 'Missing deploy.env'; exit 1; }; docker compose --env-file deploy.env -f docker-compose.yml up -d"

echo "==> Deployment finished"
"${SSH[@]}" "$SERVER_HOST" "cd '$REMOTE_ASSETS/server' && docker compose --env-file deploy.env -f docker-compose.yml ps"
