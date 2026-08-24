#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCHMARK_DIR="$ROOT/benchmarks/server"
ENVS_DIR="$BENCHMARK_DIR/.venvs"
RESULTS_DIR="$BENCHMARK_DIR/results"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3000}"
DURATION="${DURATION:-10s}"
THREADS="${THREADS:-2}"
CONNECTIONS="${CONNECTIONS:-${CONCURRENCY:-128}}"
RUNS="${RUNS:-7}"
WARMUP="${WARMUP:-2}"
PYTHON="${PYTHON:-3.14}"
KEEP_RAW="${KEEP_RAW:-0}"
WRK="${WRK:-wrk}"
BROTLI_PKG_CONFIG="${BROTLI_PKG_CONFIG:-}"
TARGETS=(stario stario-cython fastapi blacksheep-uvicorn blacksheep-granian sanic)
ENDPOINTS=(plaintext json params validate)

RUN_DIR=""
SERVER_PID=""
SERVER_PORT=""
SERVER_CMD=()

usage() {
  cat <<'EOF'
Usage: benchmarks/server/run.sh [stario|stario-cython|fastapi|blacksheep-uvicorn|blacksheep-granian|sanic ...]

Environment: DURATION=10s THREADS=2 CONNECTIONS=128 RUNS=7 WARMUP=2 HOST=127.0.0.1
             PORT=3000 PYTHON=3.14 REFRESH_ENVS=1 KEEP_RAW=1 WRK=wrk

Each endpoint is measured RUNS times; the first WARMUP samples are discarded,
then IQR outlier trimming is applied before reporting median RPS ± stdev.

PORT is the base port. Targets use fixed offsets: stario=PORT, stario-cython=PORT+1,
fastapi=PORT+2, blacksheep-uvicorn=PORT+3, blacksheep-granian=PORT+4, sanic=PORT+5.
EOF
}

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

known_target() {
  target_offset "$1" >/dev/null
}

target_offset() {
  local index
  for index in "${!TARGETS[@]}"; do
    if [[ "${TARGETS[$index]}" == "$1" ]]; then
      echo "$index"
      return 0
    fi
  done
  return 1
}

path_for() {
  case "$1" in
    plaintext) echo /plaintext ;;
    json) echo /json ;;
    params) echo /user/42 ;;
    validate) echo /validate ;;
  esac
}

port_for() {
  echo $((PORT + $(target_offset "$1")))
}

port_list() {
  local target parts=()
  for target in "${TARGETS[@]}"; do
    parts+=("$target:$(port_for "$target")")
  done
  local IFS=,
  echo "${parts[*]}"
}

format_int() {
  local value="$1" out=""
  while ((${#value} > 3)); do
    out=",${value:${#value}-3:3}$out"
    value="${value:0:${#value}-3}"
  done
  printf '%s%s' "$value" "$out"
}

python_for() { echo "$ENVS_DIR/$1/bin/python"; }

require_port_free() {
  local python="$1" target="$2" port="$3"
  if ! "$python" - "$HOST" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
family = socket.AF_INET6 if ":" in host else socket.AF_INET

with socket.socket(family, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
PY
  then
    echo "Port $HOST:$port is already in use before starting $target." >&2
    echo "Stop the existing server or run with a different PORT=... value." >&2
    exit 1
  fi
}

ensure_env() {
  local name="$1" python
  shift
  python="$(python_for "$name")"
  [[ "${REFRESH_ENVS:-0}" == 1 ]] && rm -rf "$ENVS_DIR/$name"
  if [[ ! -x "$python" ]]; then
    uv venv "$ENVS_DIR/$name" --python "$PYTHON"
    uv pip install --python "$python" "$@"
  fi
}

build_stario_cython() {
  local python="$1"
  local pkg_config_path="$BROTLI_PKG_CONFIG"
  if [[ -z "$pkg_config_path" ]]; then
    for candidate in \
      /tmp/brotli-install/lib/pkgconfig \
      /usr/lib/x86_64-linux-gnu/pkgconfig \
      /usr/local/lib/pkgconfig; do
      if [[ -f "$candidate/libbrotlienc.pc" ]]; then
        pkg_config_path="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$pkg_config_path" ]] || ! PKG_CONFIG_PATH="$pkg_config_path" pkg-config --exists libbrotlienc libbrotlicommon; then
    echo "Missing Brotli development libraries for stario-cython." >&2
    echo "Install libbrotli-dev or set BROTLI_PKG_CONFIG to a directory with libbrotlienc.pc." >&2
    exit 1
  fi
  echo "  building stario_cython extensions (PKG_CONFIG_PATH=$pkg_config_path)"
  (cd "$ROOT" && PKG_CONFIG_PATH="$pkg_config_path" "$python" setup.py)
}

ensure_target() {
  case "$1" in
    stario) ensure_env stario "stario @ file://$ROOT" uvloop ujson ;;
    stario-cython)
      ensure_env stario-cython "stario @ file://$ROOT" uvloop ujson cython setuptools wheel
      build_stario_cython "$(python_for stario-cython)"
      ;;
    fastapi) ensure_env fastapi fastapi 'uvicorn[standard]' ujson ;;
    blacksheep-uvicorn) ensure_env blacksheep-uvicorn blacksheep 'uvicorn[standard]' ujson ;;
    blacksheep-granian) ensure_env blacksheep-granian blacksheep granian uvloop ujson ;;
    sanic) ensure_env sanic sanic uvloop ujson ;;
  esac
}

uvicorn_cmd() {
  local env_name="$1" app="$2"
  SERVER_CMD=(
    "$(python_for "$env_name")" -m uvicorn "$app"
    --host "$HOST" --port "$SERVER_PORT"
    --workers 1
    --loop uvloop
    --http httptools
    --no-access-log
    --log-level warning
  )
}

command_for() {
  case "$1" in
    stario|stario-cython)
      export STARIO_HOST="$HOST"
      export STARIO_PORT="$SERVER_PORT"
      export STARIO_LOOP=uvloop
      export STARIO_TRACER=noop
      export STARIO_COMPRESS_ZSTD_LEVEL=-1
      export STARIO_COMPRESS_BROTLI_LEVEL=-1
      export STARIO_COMPRESS_GZIP_LEVEL=-1
      if [[ "$1" == stario-cython ]]; then
        SERVER_CMD=(
          env PYTHONPATH="$ROOT/src:$BENCHMARK_DIR"
          "$(python_for stario-cython)" -m stario_cython apps.stario_app:bootstrap
        )
      else
        SERVER_CMD=(
          "$(python_for stario)" -m stario.cli serve apps.stario_app:bootstrap
        )
      fi
      ;;
    fastapi)
      uvicorn_cmd fastapi apps.fastapi_app:app
      ;;
    blacksheep-uvicorn)
      uvicorn_cmd blacksheep-uvicorn apps.blacksheep_app:app
      ;;
    blacksheep-granian)
      SERVER_CMD=(
        "$(python_for blacksheep-granian)" -m granian apps.blacksheep_app:app
        --interface asgi
        --host "$HOST" --port "$SERVER_PORT"
        --workers 1
        --runtime-threads 1
        --loop uvloop
        --no-access-log
        --log-level warning
      )
      ;;
    sanic)
      SERVER_CMD=(
        "$(python_for sanic)" "$BENCHMARK_DIR/apps/sanic_app.py"
        --host "$HOST" --port "$SERVER_PORT"
      )
      ;;
  esac
}

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  SERVER_PID=""
}

fail_with_log() {
  local message="$1" log="$2"
  echo "$message Log: $log" >&2
  sed -n '1,120p' "$log" >&2 || true
  exit 1
}

wait_ready() {
  local url="http://$HOST:$SERVER_PORT/plaintext" deadline=$((SECONDS + 10)) log="$1"
  until curl -fsS --max-time 1 "$url" >/dev/null 2>&1; do
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      fail_with_log "Server exited before it was ready." "$log"
    fi
    if ((SECONDS >= deadline)); then
      fail_with_log "Server did not become ready at $url." "$log"
    fi
    sleep 0.1
  done
  if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    fail_with_log "Server exited after readiness check." "$log"
  fi
}

start_server() {
  local target="$1" log="$RUN_DIR/$target.server.log"
  command_for "$target"
  echo "+ ${SERVER_CMD[*]}"
  (cd "$ROOT" && exec env PYTHONPATH="$BENCHMARK_DIR" "${SERVER_CMD[@]}") >"$log" 2>&1 &
  SERVER_PID="$!"
  wait_ready "$log"
}

aggregate_samples() {
  local stats_file="$1" python="$2"
  "$python" "$BENCHMARK_DIR/stats.py" >"$stats_file"
}

run_endpoint() {
  local target="$1" endpoint="$2" venv_python samples_file stats_file sample raw_out run_idx total
  local url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
  local cmd=("$WRK" -t "$THREADS" -c "$CONNECTIONS" -d "$DURATION")
  [[ "$endpoint" == validate ]] && cmd+=(-s "$BENCHMARK_DIR/validate.lua")

  samples_file="$RUN_DIR/$target.$endpoint.samples"
  stats_file="$RUN_DIR/$target.$endpoint.stats.json"
  : >"$samples_file"
  venv_python="$(python_for "$target")"

  total=$((RUNS + WARMUP))
  echo "  $endpoint ($RUNS measured runs, $WARMUP warmup discarded)"
  for ((run_idx = 1; run_idx <= total; run_idx++)); do
    raw_out="$RUN_DIR/$target.$endpoint.run${run_idx}.txt"
    "${cmd[@]}" "$url" >"$raw_out"
    sample="$(awk '/Requests\/sec:/ {print $2; exit}' "$raw_out")"
    [[ -n "$sample" ]] || { echo "Failed to parse wrk output: $raw_out" >&2; exit 1; }
    if ((run_idx > WARMUP)); then
      echo "$sample" >>"$samples_file"
    fi
    if [[ "$KEEP_RAW" != 1 ]]; then
      rm -f "$raw_out"
    fi
  done

  aggregate_samples "$stats_file" "$venv_python" <"$samples_file"
  "$venv_python" -c 'import json,sys; print(f"{json.load(sys.stdin)["median"]:.2f}")' <"$stats_file" >"$RUN_DIR/$target.$endpoint.rps"
}

run_target() {
  local target="$1" endpoint
  echo; echo "== $target =="
  ensure_target "$target"
  SERVER_PORT="$(port_for "$target")"
  require_port_free "$(python_for "$target")" "$target" "$SERVER_PORT"
  echo "  port $SERVER_PORT"
  start_server "$target"
  for endpoint in "${ENDPOINTS[@]}"; do run_endpoint "$target" "$endpoint"; done
  stop_server
}

format_cell() {
  local stats_file="$1" python="$2"
  "$python" - "$stats_file" <<'PY'
import json
import pathlib
import sys

stats = json.loads(pathlib.Path(sys.argv[1]).read_text())

def fmt(value: float) -> str:
    return f"{value:,.0f}"

median = fmt(stats["median"])
stdev = fmt(stats["stdev"])
outliers = stats["outliers_removed"]
cell = f"{median} ± {stdev}"
if outliers:
    cell += f" ({outliers} outlier{'s' if outliers != 1 else ''} removed)"
print(cell)
PY
}

print_summary() {
  local target endpoint stats_file table="$RUN_DIR/summary.md" venv_python
  : >"$table"
  {
    echo "## Single-worker framework comparison"
    echo
    echo "Median requests/sec ± sample stdev after discarding $WARMUP warmup run(s)"
    echo "and applying IQR outlier trimming across $RUNS measured samples per endpoint."
    echo
    echo "| Target | Plaintext | JSON | Params | Validate |"
    echo "| --- | ---: | ---: | ---: | ---: |"
    for target in "$@"; do
      venv_python="$(python_for "$target")"
      printf '| %s ' "$target"
      for endpoint in "${ENDPOINTS[@]}"; do
        stats_file="$RUN_DIR/$target.$endpoint.stats.json"
        if [[ -f "$stats_file" ]]; then
          cell="$(format_cell "$stats_file" "$venv_python")"
          printf '| %s ' "$cell"
        else
          printf '| - '
        fi
      done
      echo "|"
    done
  } | tee "$table"
  rm -f "$RUN_DIR"/*.rps "$RUN_DIR"/*.samples
}

cleanup_raw_outputs() {
  local target endpoint
  [[ "$KEEP_RAW" == 1 ]] && return 0

  for target in "$@"; do
    rm -f "$RUN_DIR/$target.server.log"
    for endpoint in "${ENDPOINTS[@]}"; do
      rm -f "$RUN_DIR/$target.$endpoint.txt" "$RUN_DIR/$target.$endpoint.run"*.txt
      rm -f "$RUN_DIR/$target.$endpoint.stats.json"
    done
  done
}

main() {
  need uv; need curl
  if ! command -v "$WRK" >/dev/null 2>&1; then
    echo "Missing wrk executable: $WRK" >&2
    echo "Install wrk or set WRK=/path/to/wrk." >&2
    exit 1
  fi
  trap stop_server EXIT INT TERM

  local selected=("$@") target
  if ((${#selected[@]} == 0)); then selected=("${TARGETS[@]}"); fi
  case "${selected[0]}" in -h|--help|help) usage; exit 0 ;; esac
  for target in "${selected[@]}"; do known_target "$target" || { echo "Unknown target: $target" >&2; usage >&2; exit 1; }; done

  RUN_DIR="$RESULTS_DIR/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$RUN_DIR"
  cat >"$RUN_DIR/config.txt" <<EOF
host=$HOST
base_port=$PORT
duration=$DURATION
threads=$THREADS
connections=$CONNECTIONS
runs=$RUNS
warmup=$WARMUP
python=$PYTHON
client=$WRK
keep_raw=$KEEP_RAW
git_sha=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
git_branch=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
payload_file=benchmarks/server/validate.lua
ports=$(port_list)
EOF

  echo "Writing results to ${RUN_DIR#$ROOT/}"
  for target in "${selected[@]}"; do run_target "$target"; done
  echo; print_summary "${selected[@]}"
  cleanup_raw_outputs "${selected[@]}"
  echo; echo "Summary: ${RUN_DIR#$ROOT/}/summary.md"
  if [[ "$KEEP_RAW" == 1 ]]; then echo "Raw output: ${RUN_DIR#$ROOT/}/*.txt"; fi
}

main "$@"
