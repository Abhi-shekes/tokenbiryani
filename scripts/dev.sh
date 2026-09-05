#!/usr/bin/env bash
# Bring the whole thing up locally, without Docker: a mock Anthropic on :9911, and
# the gateway in front of it. `docker compose up` is the other way to do this; this
# one is for when you would rather not involve a container.
#
# tokenbiryani.yaml is gitignored — it is where real credentials go — so a fresh
# clone has none, and this writes a mock-backed one on first run.
#
# That config points its accounts at 127.0.0.1:9911, so without the mock running
# every request connection-refuses and the console looks broken. This script is the
# missing half.
#
#   scripts/dev.sh            # mock + gateway
#   scripts/dev.sh --no-mock  # gateway only, for when you point the config at real keys
#
# Nothing here reaches Anthropic and nothing costs money.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

UPSTREAM_PORT=${UPSTREAM_PORT:-9911}
GATEWAY_PORT=${GATEWAY_PORT:-8787}
WITH_MOCK=1
[ "${1:-}" = "--no-mock" ] && WITH_MOCK=0

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  sleep 0.3
  for pid in "${PIDS[@]:-}"; do kill -9 "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- 3<&-; }

# A fresh clone has no tokenbiryani.yaml. Write one pointed at the mock, so this
# script works immediately instead of failing on a file the repo cannot ship.
if [ ! -f tokenbiryani.yaml ]; then
  KEY="bir_dev_$("$PY" -c 'import secrets;print(secrets.token_urlsafe(18))')"
  cat > tokenbiryani.yaml <<YAML
# Written by scripts/dev.sh. Gitignored: this is where real credentials go.
# The three accounts below point at the bundled mock upstream, so nothing here
# reaches Anthropic and nothing costs money. Replace them, or add real accounts
# from the console.
server: {host: 127.0.0.1, port: $GATEWAY_PORT}
routing: {strategy: sticky_headroom}
store: {backend: sqlite, path: manual-test.db, secret_key_path: manual-test.key}
accounts:
  - {id: acct-01, name: Mock 1, type: anthropic_api, api_key: key-01, base_url: "http://127.0.0.1:$UPSTREAM_PORT"}
  - {id: acct-02, name: Mock 2, type: anthropic_api, api_key: key-02, base_url: "http://127.0.0.1:$UPSTREAM_PORT"}
  - {id: acct-03, name: Mock 3, type: anthropic_api, api_key: key-03, base_url: "http://127.0.0.1:$UPSTREAM_PORT", cost_tier: 2.5}
keys:
  - {key: $KEY, name: admin, admin: true}
# The dated table that ships with the release, so the cost charts show real rates
# rather than invented ones.
pricing: builtin
YAML
  echo "  wrote tokenbiryani.yaml    (mock-backed; gitignored)"
fi

wait_for() {
  # Any HTTP answer means it is listening. Not -f: the mock authenticates
  # /v1/models, so a keyless probe there is a 401 from a healthy server.
  local url=$1 name=$2 tries=0
  until curl -s -o /dev/null "$url"; do
    tries=$((tries + 1))
    if [ $tries -gt 60 ]; then echo "  $name never came up at $url" >&2; exit 1; fi
    sleep 0.25
  done
}

if [ "$WITH_MOCK" = 1 ]; then
  if listening "$UPSTREAM_PORT"; then
    echo "  mock upstream        already listening on :$UPSTREAM_PORT — reusing it"
  else
    # These keys are the ones tokenbiryani.yaml hands to its three accounts.
    # From the source tree: the fake upstream is test scaffolding and is no longer
    # part of the installed package.
    PYTHONPATH="$ROOT/tests" "$PY" -m support.server --port "$UPSTREAM_PORT" \
      --accounts key-01,key-02,key-03 >/tmp/tokenbiryani-mock.log 2>&1 &
    PIDS+=($!)
    wait_for "http://127.0.0.1:$UPSTREAM_PORT/v1/models" "mock upstream"
    echo "  mock upstream        http://127.0.0.1:$UPSTREAM_PORT  (log: /tmp/tokenbiryani-mock.log)"
  fi
fi

if listening "$GATEWAY_PORT"; then
  echo "  something is already listening on :$GATEWAY_PORT — stop it and retry" >&2
  exit 1
fi

ADMIN_KEY=$("$PY" - <<'PY'
import yaml
with open("tokenbiryani.yaml") as handle:
    config = yaml.safe_load(handle) or {}
for entry in config.get("keys") or []:
    if entry.get("admin"):
        print(entry.get("key", ""))
        break
PY
)

echo "  console              http://127.0.0.1:$GATEWAY_PORT/console"
[ -n "$ADMIN_KEY" ] && echo "  admin key            $ADMIN_KEY"
echo
echo "  export ANTHROPIC_BASE_URL=http://127.0.0.1:$GATEWAY_PORT"
[ -n "$ADMIN_KEY" ] && echo "  export ANTHROPIC_AUTH_TOKEN=$ADMIN_KEY"
echo

exec "$PY" -m tokenbiryani.cli serve --port "$GATEWAY_PORT" "${@:2}"
