#!/usr/bin/env bash
# Fair head-to-head: same VM, same wrk, clean ports, interleaved samples.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="/workspace"
BENCH="$ROOT/benchmarks/server"
ENVS="$BENCH/.venvs-cloud-fair"
REPS="${REPS:-5}"
DURATION="${DURATION:-15s}"
WRK_T="${WRK_T:-2}"
WRK_C="${WRK_C:-128}"
HOST="127.0.0.1"
BASE_PORT=3700
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BENCH/results-cloud/fair-$STAMP"
mkdir -p "$OUT"

log() { echo "[$(date -u +%H:%M:%S)] $*" >>"$OUT/journal.log"; echo "[$(date -u +%H:%M:%S)] $*" >&2; }

need() { command -v "$1" >/dev/null || { echo "missing $1" >&2; exit 1; }; }
need uv wrk curl fuser python3

ENDPOINTS=(plaintext json params validate)
TARGETS=(stario-314t stario-314 blacksheep-granian)

python_for() {
  case "$1" in
    stario-314t) echo "$ENVS/stario-314t/bin/python" ;;
    stario-314) echo "$ENVS/stario-314/bin/python" ;;
    blacksheep-granian) echo "$ENVS/blacksheep-granian/bin/python" ;;
    *) return 1 ;;
  esac
}

port_for() {
  case "$1" in
    stario-314t) echo $((BASE_PORT + 0)) ;;
    stario-314) echo $((BASE_PORT + 1)) ;;
    blacksheep-granian) echo $((BASE_PORT + 2)) ;;
  esac
}

path_for() {
  case "$1" in
    plaintext) echo /plaintext ;;
    json) echo /json ;;
    params) echo /user/42 ;;
    validate) echo /validate ;;
  esac
}

ensure_envs() {
  uv python install 3.14 3.14t >/dev/null 2>&1 || true
  if [[ ! -x "$ENVS/stario-314t/bin/python" ]]; then
    uv venv "$ENVS/stario-314t" --python 3.14t
    uv pip install --python "$ENVS/stario-314t/bin/python" stario==5.0.0 uvloop ujson
  fi
  if [[ ! -x "$ENVS/stario-314/bin/python" ]]; then
    uv venv "$ENVS/stario-314" --python 3.14
    uv pip install --python "$ENVS/stario-314/bin/python" stario==5.0.0 uvloop ujson
  fi
  if [[ ! -x "$ENVS/blacksheep-granian/bin/python" ]]; then
    uv venv "$ENVS/blacksheep-granian" --python 3.14
    uv pip install --python "$ENVS/blacksheep-granian/bin/python" blacksheep granian uvloop ujson
  fi
}

SERVER_PID=""
SERVER_PORT=""

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    local d=$((SECONDS + 8))
    while kill -0 "$SERVER_PID" 2>/dev/null && ((SECONDS < d)); do sleep 0.1; done
    kill -9 "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "${SERVER_PORT:-}" ]]; then
    fuser -k "${SERVER_PORT}/tcp" 2>/dev/null || true
    sleep 0.4
  fi
  SERVER_PID=""
}

verify_response() {
  local target="$1" url="http://${HOST}:${SERVER_PORT}/plaintext"
  local body
  body="$(curl -fsS --max-time 2 "$url")" || return 1
  case "$target" in
    stario-*) [[ "$body" == "Hello, World!" ]] || { log "BAD BODY stario: $body"; return 1; } ;;
    blacksheep-granian) [[ -n "$body" ]] || return 1 ;;
  esac
}

start_target() {
  local target="$1"
  local py log="$OUT/${target}.log"
  py="$(python_for "$target")"
  SERVER_PORT="$(port_for "$target")"
  stop_server
  log "start $target port=$SERVER_PORT py=$($py -V 2>&1)"
  case "$target" in
    stario-314t|stario-314)
      (
        cd "$ROOT"
        export STARIO_HOST="$HOST" STARIO_PORT="$SERVER_PORT" STARIO_TRACER=noop
        export STARIO_COMPRESS_ZSTD_LEVEL=-1 STARIO_COMPRESS_BROTLI_LEVEL=-1 STARIO_COMPRESS_GZIP_LEVEL=-1
        exec env PYTHONPATH="$BENCH" "$py" -c \
          "import uvloop; from stario import serve; from apps.stario_app import bootstrap; uvloop.run(serve(bootstrap))"
      ) >"$log" 2>&1 &
      ;;
    blacksheep-granian)
      (
        cd "$ROOT"
        exec env PYTHONPATH="$BENCH" "$py" -m granian apps.blacksheep_app:app \
          --interface asgi --host "$HOST" --port "$SERVER_PORT" \
          --workers 1 --runtime-threads 1 --loop uvloop --no-access-log --log-level warning
      ) >"$log" 2>&1 &
      ;;
  esac
  SERVER_PID=$!
  local deadline=$((SECONDS + 25))
  until curl -fsS --max-time 1 "http://${HOST}:${SERVER_PORT}/plaintext" >/dev/null 2>&1; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      log "EXITED $target"; tail -40 "$log" | tee -a "$OUT/journal.log"; return 1
    fi
    ((SECONDS >= deadline)) && { log "TIMEOUT $target"; tail -20 "$log"; return 1; }
    sleep 0.15
  done
  verify_response "$target" || return 1
}

wrk_once() {
  local endpoint="$1"
  local url="http://${HOST}:${SERVER_PORT}$(path_for "$endpoint")"
  local -a cmd=(wrk -t "$WRK_T" -c "$WRK_C" -d "$DURATION")
  [[ "$endpoint" == validate ]] && cmd+=(-s "$BENCH/validate.lua")
  "${cmd[@]}" "$url" 2>/dev/null | awk '/Requests\/sec:/ {print $2; exit}'
}

run_block() {
  local target="$1" endpoint="$2" rep="$3"
  start_target "$target" || exit 1
  wrk_once "$endpoint"
  stop_server
  sleep 1
}

{
  echo "stamp=$STAMP"
  echo "reps=$REPS duration=$DURATION wrk=-t$WRK_T -c$WRK_C"
  uname -a
  nproc
  lscpu | awk -F: '/Model name/{print $2}'
  free -h | head -2
} >"$OUT/config.txt"

ensure_envs
trap stop_server EXIT INT TERM

# Interleaved: round-robin targets × endpoints × reps (same load pattern for each).
: >"$OUT/raw.tsv"
log "interleaved run: targets=${TARGETS[*]} reps=$REPS"

for ((rep=1; rep<=REPS; rep++)); do
  for endpoint in "${ENDPOINTS[@]}"; do
    for target in "${TARGETS[@]}"; do
      rps="$(run_block "$target" "$endpoint" "$rep")"
      log "rep=$rep endpoint=$endpoint target=$target rps=$rps"
      printf '%s\t%s\t%s\t%s\n' "$rep" "$target" "$endpoint" "$rps" >>"$OUT/raw.tsv"
    done
  done
done

python3 - "$OUT" <<'PY'
import sys, statistics
from collections import defaultdict
out = sys.argv[1]
acc = defaultdict(list)
for line in open(f"{out}/raw.tsv"):
    rep, target, endpoint, rps = line.strip().split("\t")
    acc[(target, endpoint)].append(float(rps))

lines = [
    "# Fair comparison (interleaved on one VM)",
    "",
    "| Target | Plaintext | JSON | Params | Validate |",
    "| --- | ---: | ---: | ---: | ---: |",
]
order = ["stario-314t", "stario-314", "blacksheep-granian"]
labels = {
    "stario-314t": "Stario 5.0 (3.14t, uvloop)",
    "stario-314": "Stario 5.0 (3.14 GIL, uvloop)",
    "blacksheep-granian": "BlackSheep 2.6 + Granian 2.8",
}
for target in order:
    row = [labels[target]]
    for ep in ["plaintext", "json", "params", "validate"]:
        vals = acc[(target, ep)]
        m = statistics.mean(vals)
        s = statistics.stdev(vals) if len(vals) > 1 else 0
        row.append(f"{m:,.0f} ± {s:,.0f}")
    lines.append("| " + " | ".join(row) + " |")

open(f"{out}/summary.md", "w").write("\n".join(lines) + "\n")
print(open(f"{out}/summary.md").read())
PY

log "done -> $OUT/summary.md"
