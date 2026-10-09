#!/usr/bin/env bash
# Health gate for the plugin_parser stack. Runs ON the deployment host.
#
# release.sh uses this to decide whether a rollout actually succeeded; it is
# also safe (and useful) to run by hand after any manual change.
#
#   bash verify.sh                          # wait up to 240s
#   VERIFY_TIMEOUT=60 bash verify.sh        # fail fast
#   REQUIRE_MOSS_LOADED=1 bash verify.sh    # also wait for the model to load
#
# Exits non-zero on the first hard requirement that is not met.
set -uo pipefail

cd "$(dirname "$0")" || exit 1

if [ ! -f deploy.env ]; then
  echo "verify: deploy.env not found in $(pwd)" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./deploy.env
set +a

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
MOSS_PORT="${MOSS_PORT:-9000}"
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-240}"
REQUIRE_MOSS_LOADED="${REQUIRE_MOSS_LOADED:-0}"
EXPECTED_TAG="${IMAGE_TAG:-}"

API_URL="http://127.0.0.1:${API_PORT}"
WEB_URL="http://127.0.0.1:${WEB_PORT}"
MOSS_URL="http://127.0.0.1:${MOSS_PORT}"

fail() { echo "verify: FAIL - $*" >&2; exit 1; }
ok()   { echo "  ok   $*"; }
note() { echo "       $*"; }
warn() { echo "  warn $*"; }

echo "==> containers (expected tag: ${EXPECTED_TAG:-<unset>})"
for name in plugin_parser-postgres plugin_parser-api plugin_parser-moss plugin_parser-web; do
  state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)" \
    || fail "$name not found - has the stack ever been deployed?"
  [ "$state" = "running" ] || fail "$name is '$state', expected 'running'"
  ok "$name running"
done

# The running image must be the tag we just released. Without this a stale
# container happily passes every endpoint check while serving old code.
if [ -n "$EXPECTED_TAG" ]; then
  for svc in api moss web; do
    img="$(docker inspect -f '{{.Config.Image}}' "plugin_parser-$svc" 2>/dev/null)"
    case "$img" in
      *":$EXPECTED_TAG") ok "plugin_parser-$svc -> $img" ;;
      *) fail "plugin_parser-$svc is running '$img', expected tag $EXPECTED_TAG" ;;
    esac
  done
fi

# MOSS reads uploaded audio by path, so it must see the shared media volume.
# Losing this mount silently breaks every transcription.
for spec in "plugin_parser-moss:/data" "plugin_parser-moss:/models" "plugin_parser-api:/data"; do
  cname="${spec%%:*}"
  dest="${spec##*:}"
  mounts="$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$cname" 2>/dev/null)"
  case " $mounts " in
    *" $dest "*) ok "$cname has $dest mounted" ;;
    *) fail "$cname is missing the $dest mount (see docker-compose.yml)" ;;
  esac
done

echo "==> API health: ${API_URL}/api/system/health"
api_json=""
deadline=$(( $(date +%s) + VERIFY_TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if api_json="$(curl -sf -m 5 "$API_URL/api/system/health" 2>/dev/null)" && [ -n "$api_json" ]; then
    break
  fi
  api_json=""
  sleep 3
done
[ -n "$api_json" ] || fail "API did not answer within ${VERIFY_TIMEOUT}s"

echo "$api_json" | grep -q '"status":"ok"'            || fail "API status is not ok: $api_json"
echo "$api_json" | grep -q '"database":"connected"'   || fail "API cannot reach the database: $api_json"
echo "$api_json" | grep -q '"moss_reachable":true'    || fail "API cannot reach MOSS: $api_json"
echo "$api_json" | grep -q '"moss_model_found":true'  || fail "MOSS model files are missing: $api_json"
echo "$api_json" | grep -q '"summary_model_found":true' || fail "summary model file is missing: $api_json"
ok "database connected, MOSS reachable, both models present"

# The engine may still be warming up; that is not fatal for the rollout but
# it is worth knowing about, since summaries/transcriptions need it.
if echo "$api_json" | grep -q '"moss_model_loaded":true'; then
  ok "MOSS model loaded (device=$(echo "$api_json" | sed -n 's/.*"moss_device":"\([^"]*\)".*/\1/p'))"
elif [ "$REQUIRE_MOSS_LOADED" = "1" ]; then
  fail "MOSS model still not loaded after ${VERIFY_TIMEOUT}s: $api_json"
else
  warn "MOSS model not loaded yet (still warming up); set REQUIRE_MOSS_LOADED=1 to gate on it"
fi

echo "==> web: ${WEB_URL}/"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$WEB_URL/" 2>/dev/null)"
[ "$code" = "200" ] || fail "web returned HTTP $code"
ok "web returned 200"

echo "==> MOSS loopback: ${MOSS_URL}/health"
curl -sf -m 10 "$MOSS_URL/health" >/dev/null 2>&1 || fail "MOSS /health not reachable on loopback"
ok "MOSS /health reachable (loopback only, not exposed publicly)"

# Guard the isolation contract: MOSS must never be published on a public
# interface, and Postgres must not be published at all.
moss_ports="$(docker port plugin_parser-moss 2>/dev/null | tr -d ' ')"
case "$moss_ports" in
  *"0.0.0.0"*) fail "MOSS is published on 0.0.0.0 ($moss_ports); it must stay on 127.0.0.1" ;;
  *) ok "MOSS port binding: ${moss_ports:-none}" ;;
esac
if [ -n "$(docker port plugin_parser-postgres 2>/dev/null)" ]; then
  fail "Postgres is published on the host; it must stay internal to plugin_parser_net"
fi
ok "Postgres not published on the host"

echo "verify: PASS (tag=${EXPECTED_TAG:-<unset>})"
