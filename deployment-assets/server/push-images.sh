#!/usr/bin/env bash
set -euo pipefail

SERVER_HOST="${SERVER_HOST:?Set SERVER_HOST, for example root@8.136.26.124}"
SERVER_PORT="${SERVER_PORT:-22}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_ASSETS="${REMOTE_ASSETS:-/opt/plugin_parser}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGES="$ROOT/deployment-assets/images"
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" -p "$SERVER_PORT")
SCP=(scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "$SSH_KEY" -P "$SERVER_PORT")

for archive in api.tar.gz web.tar.gz moss.tar.gz; do
  test -f "$IMAGES/$archive" || { echo "Missing $IMAGES/$archive; run build-images.sh first"; exit 1; }
done

"${SSH[@]}" "$SERVER_HOST" "mkdir -p '$REMOTE_ASSETS/images'"
echo "==> Uploading image archives"
"${SCP[@]}" "$IMAGES"/*.tar.gz "$SERVER_HOST:$REMOTE_ASSETS/images/"
echo "==> Loading images on server"
"${SSH[@]}" "$SERVER_HOST" "for archive in '$REMOTE_ASSETS'/images/*.tar.gz; do gzip -dc \"\$archive\" | docker load; done"
