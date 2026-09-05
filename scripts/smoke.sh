#!/usr/bin/env bash
# End-to-end smoke test: a real gateway process in front of a real mock upstream.
# Proves the thing works over sockets, not just through an in-process transport.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
export PYTHONPATH="$ROOT/src"
UPSTREAM_PORT=${UPSTREAM_PORT:-9911}
GATEWAY_PORT=${GATEWAY_PORT:-8798}
KEY="bir_smoketest_key_0123456789"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  # Insist, then reap. A survivor would answer the next run's checks and quietly
  # test the wrong build.
  sleep 0.5
  for pid in "${PIDS[@]:-}"; do
    kill -9 "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# A leftover process from an earlier run would answer every check and quietly test
# the wrong build. Refuse to start rather than report a misleading pass.
port_free() {
  local port=$1 name=$2
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "FAIL: something is already listening on $port ($name). Stop it and retry."
    exit 1
  fi
}

wait_for() {
  # Any HTTP answer means the process is listening, which is all this asks. Insisting
  # on 2xx would make readiness depend on the endpoint's own auth: the mock upstream
  # authenticates /v1/models, so a keyless probe there is a 401 from a healthy server.
  local url=$1 name=$2 tries=0
  until curl -s -o /dev/null "$url"; do
    tries=$((tries + 1))
    if [ $tries -gt 60 ]; then echo "FAIL: $name never came up at $url"; exit 1; fi
    sleep 0.25
  done
}

say() { printf '  %-46s %s\n' "$1" "$2"; }

# Substring tests without a pipe.
#
# `echo "$BIG" | grep -q needle` is a trap under `set -o pipefail`: grep -q exits the
# moment it matches, and if the value is larger than the pipe buffer the echo still
# writing into it dies of SIGPIPE. pipefail then reports 141 for a pipeline that
# matched, so the check fails intermittently — on size, which is why it only started
# happening when the console grew past 64KB. Bash's own matching has no pipe and no
# race.
contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

echo
echo "smoke: mock upstream :$UPSTREAM_PORT -> gateway :$GATEWAY_PORT"
echo

port_free "$UPSTREAM_PORT" "mock upstream"
port_free "$GATEWAY_PORT" "gateway"

# The fake upstream is test scaffolding, not part of the installed package, so it is
# run from the source tree rather than imported from wherever tokenbiryani installed.
PYTHONPATH="$ROOT/tests" python3 -m support.server \
  --port "$UPSTREAM_PORT" --accounts key-a,key-b \
  >"$WORK/upstream.log" 2>&1 &
PIDS+=($!)

cat > "$WORK/tokenbiryani.yaml" <<YAML
server:
  host: 127.0.0.1
  port: $GATEWAY_PORT
routing:
  strategy: sticky_headroom
accounts:
  - id: acct-01
    type: anthropic_api
    api_key: key-a
    base_url: http://127.0.0.1:$UPSTREAM_PORT
  - id: acct-02
    type: anthropic_api
    api_key: key-b
    base_url: http://127.0.0.1:$UPSTREAM_PORT
keys:
  - key: $KEY
    name: smoke
    admin: true
YAML

wait_for "http://127.0.0.1:$UPSTREAM_PORT/v1/models" "mock upstream"

(cd "$WORK" && exec python3 -m tokenbiryani.cli serve --log-level warning \
  >"$WORK/gateway.log" 2>&1) &
PIDS+=($!)
wait_for "http://127.0.0.1:$GATEWAY_PORT/healthz" "gateway"

fail() { echo "FAIL: $1"; echo "--- gateway log ---"; cat "$WORK/gateway.log"; exit 1; }

# 1. a plain request
BODY='{"model":"claude-test-1","max_tokens":64,"messages":[{"role":"user","content":"hello"}]}'
OUT=$(curl -sS -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/messages" \
  -H "x-api-key: $KEY" -H 'content-type: application/json' -d "$BODY")
contains "$OUT" '"role": "assistant"' || contains "$OUT" '"role":"assistant"' \
  || fail "non-streaming request: $OUT"
say "non-streaming request" "ok"

# 2. the account that served it is reported
ACCT=$(curl -sS -D - -o /dev/null -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/messages" \
  -H "x-api-key: $KEY" -H 'content-type: application/json' -d "$BODY" \
  | tr -d '\r' | awk 'tolower($1)=="x-tokenbiryani-account:"{print $2}')
[ -n "$ACCT" ] || fail "no x-tokenbiryani-account header"
say "routed to an account" "$ACCT"

# 3. streaming
SBODY='{"model":"claude-test-1","max_tokens":64,"stream":true,"messages":[{"role":"user","content":"hi"}]}'
STREAM=$(curl -sS -N -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/messages" \
  -H "x-api-key: $KEY" -H 'content-type: application/json' -d "$SBODY")
contains "$STREAM" 'event: content_block_delta' || fail "streaming: $STREAM"
contains "$STREAM" 'event: message_stop' || fail "stream did not finish"
contains "$STREAM" 'event: error' && fail "unexpected error frame in stream"
say "streaming request" "ok"

# 4. affinity: the same conversation stays on one account
FIRST=""
for _ in 1 2 3 4; do
  A=$(curl -sS -D - -o /dev/null -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/messages" \
    -H "x-api-key: $KEY" -H 'content-type: application/json' -d "$BODY" \
    | tr -d '\r' | awk 'tolower($1)=="x-tokenbiryani-account:"{print $2}')
  [ -z "$FIRST" ] && FIRST="$A"
  [ "$A" = "$FIRST" ] || fail "affinity broke: $FIRST then $A"
done
say "session affinity held across 4 turns" "$FIRST"

# 5. a client error is not amplified
ERR=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/messages" \
  -H "x-api-key: $KEY" -H 'content-type: application/json' -d '{"max_tokens":1}')
[ "$ERR" = "400" ] || fail "missing model should be 400, got $ERR"
say "malformed request rejected" "400"

# 6. auth
UNAUTH=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$GATEWAY_PORT/v1/messages" \
  -H 'content-type: application/json' -d "$BODY")
[ "$UNAUTH" = "401" ] || fail "unauthenticated request should be 401, got $UNAUTH"
say "unauthenticated request rejected" "401"

# 7. usage history — the accounting surface that replaced /metrics
USAGE=$(curl -sS "http://127.0.0.1:$GATEWAY_PORT/admin/usage?window=1h" -H "x-api-key: $KEY")
contains "$USAGE" '"requests"' || fail "usage history missing"
say "usage history" "ok"

# 8. the console
CONSOLE=$(curl -sS "http://127.0.0.1:$GATEWAY_PORT/console")
contains "$CONSOLE" "tokenbiryani console" || fail "console not served"
contains "$CONSOLE" "$KEY" && fail "the console shell must not contain the key"
say "console" "ok"

# 9. the CLI
STATUS=$(TOKENBIRYANI_URL="http://127.0.0.1:$GATEWAY_PORT" \
  python3 -m tokenbiryani.cli status --key "$KEY" --no-color)
contains "$STATUS" "acct-01" || fail "cli status: $STATUS"
contains "$STATUS" "ready" || fail "cli status has no ready account"
say "cli status" "ok"

echo
echo "$STATUS"
echo
echo "  all smoke checks passed"
echo
