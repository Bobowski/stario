#!/usr/bin/env bash
# Profile Stario Cython and Granian RSGI on GET /plaintext under wrk.
#
# Native speedscope (default) + optional Python-only stacks:
#   PROFILE_PYTHON=1 benchmarks/server/profile_load.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
PYSPY="${PYSPY:-$ROOT/.venv/bin/py-spy}"
if [[ ! -x "$PYSPY" ]]; then
  PYSPY="$(command -v py-spy)"
fi
WRK="${WRK:-$(command -v wrk)}"
OUT="${OUT:-$ROOT/benchmarks/server/results/profile-$(date -u +%Y%m%dT%H%M%SZ)}"
STARIO_PY="${STARIO_PY:-$ROOT/benchmarks/server/.venvs/stario-cython/bin/python}"
GRANIAN_PY="${GRANIAN_PY:-$ROOT/benchmarks/server/.venvs/granian-rsgi/bin/python}"
HOST=127.0.0.1
DURATION="${DURATION:-12s}"
THREADS=2
CONNECTIONS=128
PROFILE_PYTHON="${PROFILE_PYTHON:-1}"
RATE="${RATE:-400}"

mkdir -p "$OUT"

start_stario() {
  local port="$1"
  export STARIO_HOST="$HOST" STARIO_PORT="$port" STARIO_LOOP=uvloop STARIO_TRACER=noop
  export STARIO_COMPRESS_ZSTD_LEVEL=-1 STARIO_COMPRESS_BROTLI_LEVEL=-1 STARIO_COMPRESS_GZIP_LEVEL=-1
  unset STARIO_CYTHON_PYTHON_APP || true
  (cd "$ROOT" && exec env PYTHONPATH="$ROOT/src:$ROOT/benchmarks/server" \
    "$STARIO_PY" -m stario_cython apps.stario_app:bootstrap) \
    >"$OUT/stario.server.log" 2>&1 &
  echo $!
}

start_granian() {
  local port="$1"
  (cd "$ROOT/benchmarks/server" && exec "$GRANIAN_PY" -m granian apps.granian_rsgi_app:app \
    --interface rsgi --host "$HOST" --port "$port" --workers 1 --runtime-threads 1 \
    --loop uvloop --no-access-log --log-level warning) \
    >"$OUT/granian.server.log" 2>&1 &
  echo $!
}

wait_ready() {
  local url="$1"
  local deadline=$((SECONDS + 20))
  until curl -fsS --max-time 1 "$url" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      echo "not ready: $url" >&2
      [[ -f "$OUT/stario.server.log" ]] && tail -20 "$OUT/stario.server.log" >&2 || true
      [[ -f "$OUT/granian.server.log" ]] && tail -20 "$OUT/granian.server.log" >&2 || true
      return 1
    fi
    sleep 0.05
  done
}

# Granian may fork a worker; attach py-spy to the busiest child if present.
resolve_profile_pid() {
  local parent="$1"
  local child
  child="$(pgrep -P "$parent" 2>/dev/null | head -1 || true)"
  if [[ -n "$child" ]]; then
    echo "$child"
  else
    echo "$parent"
  fi
}

spy_record() {
  local pid="$1" dest="$2" extra=()
  shift 2
  extra=("$@")
  # Do not use --nonblocking: it produced empty captures with --native.
  "$PYSPY" record \
    --pid "$pid" \
    --subprocesses \
    --gil \
    --rate "$RATE" \
    --duration "${DURATION%s}" \
    --format speedscope \
    --output "$dest" \
    "${extra[@]}"
}

profile_one() {
  local name="$1" parent_pid="$2" port="$3"
  local pid
  pid="$(resolve_profile_pid "$parent_pid")"
  echo "== warmup $name parent=$parent_pid spy_pid=$pid =="
  "$WRK" -t "$THREADS" -c "$CONNECTIONS" -d 3s "http://$HOST:$port/plaintext" >/dev/null
  echo "== wrk+py-spy native $name =="
  spy_record "$pid" "$OUT/$name.native.speedscope.json" --native &
  local spy=$!
  "$WRK" -t "$THREADS" -c "$CONNECTIONS" -d "$DURATION" "http://$HOST:$port/plaintext" \
    | tee "$OUT/$name.wrk.txt"
  wait "$spy" || true
  if [[ "$PROFILE_PYTHON" == "1" ]]; then
    echo "== wrk+py-spy python $name =="
    spy_record "$pid" "$OUT/$name.python.speedscope.json" &
    spy=$!
    "$WRK" -t "$THREADS" -c "$CONNECTIONS" -d "$DURATION" "http://$HOST:$port/plaintext" \
      | tee "$OUT/$name.python.wrk.txt"
    wait "$spy" || true
  fi
}

STARIO_PORT="${STARIO_PORT:-4011}"
GRANIAN_PORT="${GRANIAN_PORT:-4012}"

echo "Writing profiles to $OUT"
SPID=$(start_stario "$STARIO_PORT")
wait_ready "http://$HOST:$STARIO_PORT/plaintext"
profile_one stario-cython "$SPID" "$STARIO_PORT"
kill "$SPID" 2>/dev/null || true
wait "$SPID" 2>/dev/null || true
sleep 0.3

GPID=$(start_granian "$GRANIAN_PORT")
wait_ready "http://$HOST:$GRANIAN_PORT/plaintext"
profile_one granian-rsgi "$GPID" "$GRANIAN_PORT"
kill "$GPID" 2>/dev/null || true
# granian worker children
pkill -P "$GPID" 2>/dev/null || true
wait "$GPID" 2>/dev/null || true

echo "done: $OUT"
ls -la "$OUT"
