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
BENCH_PROCS="${BENCH_PROCS:-$(nproc 2>/dev/null || echo 4)}"
TARGETS=(
  stario stario-cython stario-n stario-cython-n
  go-nethttp go-nethttp-n go-fasthttp
  socketify robyn granian-rsgi granian-rsgi-n sanic django-bolt
  blacksheep-granian blacksheep-uvicorn fastapi
)
TARGET_LABELS=(
  "stario|Stario (Python httptools)"
  "stario-cython|Stario (Cython llhttp)"
  "stario-n|Stario (Python, ${BENCH_PROCS} processes, SO_REUSEPORT)"
  "stario-cython-n|Stario (Cython, ${BENCH_PROCS} processes, SO_REUSEPORT)"
  "go-nethttp|Go net/http (GOMAXPROCS=1)"
  "go-nethttp-n|Go net/http (GOMAXPROCS=${BENCH_PROCS})"
  "go-fasthttp|Go fasthttp (GOMAXPROCS=1)"
  "socketify|Socketify (uWebSockets/libuv)"
  "robyn|Robyn (Rust/Actix)"
  "granian-rsgi|Granian RSGI (Rust, no framework)"
  "granian-rsgi-n|Granian RSGI (${BENCH_PROCS} workers)"
  "sanic|Sanic (uvloop)"
  "django-bolt|Django-Bolt (Actix/Tokio)"
  "blacksheep-granian|BlackSheep + Granian ASGI"
  "blacksheep-uvicorn|BlackSheep + Uvicorn ASGI"
  "fastapi|FastAPI + Uvicorn ASGI"
)
SUMMARY_GROUPS=(
  "Stario (this checkout)|stario stario-n stario-cython stario-cython-n"
  "Go|go-nethttp go-nethttp-n go-fasthttp"
  "Native HTTP servers|socketify robyn granian-rsgi granian-rsgi-n sanic django-bolt"
  "ASGI framework stacks|blacksheep-granian blacksheep-uvicorn fastapi"
)
READ_ENDPOINTS=(plaintext json params)
UPLOAD_ENDPOINTS=(validate post-form post-json-1k post-octet-64k post-octet-2m post-stream-2m multipart-2m)
DEFAULT_ENDPOINTS=( "${READ_ENDPOINTS[@]}" "${UPLOAD_ENDPOINTS[@]}" )
if [[ -n "${ENDPOINTS:-}" ]]; then
  ENDPOINTS_CSV="$ENDPOINTS"
else
  ENDPOINTS_CSV=""
fi
ENDPOINTS=()
UPLOAD_CONNECTIONS="${UPLOAD_CONNECTIONS:-32}"
ENDPOINT_LABELS=(
  "plaintext|Plaintext"
  "json|JSON"
  "params|Params"
  "validate|Validate JSON"
  "post-form|Form POST"
  "post-json-1k|JSON 1KB"
  "post-octet-64k|Octet 64KB"
  "post-octet-2m|Octet 2MB (buffer)"
  "post-stream-2m|Octet 2MB (stream)"
  "multipart-2m|Multipart 2MB"
)

RUN_DIR=""
SERVER_PID=""
SERVER_PIDS=()
SERVER_PORT=""
SERVER_CMD=()
SELECTED_TARGETS=()

usage() {
  cat <<'EOF'
Usage: benchmarks/server/run.sh [target ...]

Targets (default: all):
  Stario:              stario, stario-cython, stario-n, stario-cython-n
  Go:                  go-nethttp, go-nethttp-n, go-fasthttp
  Native HTTP servers: socketify, robyn, granian-rsgi, granian-rsgi-n, sanic, django-bolt
  ASGI stacks:         blacksheep-granian, blacksheep-uvicorn, fastapi

  go-nethttp / go-fasthttp pin GOMAXPROCS=1 (same 1-core budget as the
  Python/Cython one-worker rows). *-n targets fill the box: Go uses
  GOMAXPROCS=\$BENCH_PROCS in one process; stario-n / stario-cython-n start
  N processes on one port with STARIO_REUSE_PORT=1; granian-rsgi-n uses
  N workers.

Environment: DURATION=10s THREADS=2 CONNECTIONS=128 UPLOAD_CONNECTIONS=32
             RUNS=7 WARMUP=2 HOST=127.0.0.1 PORT=3000 PYTHON=3.14
             BENCH_PROCS=\$(nproc) ENDPOINT_TIER=all|read|upload
             ENDPOINTS=csv  REFRESH_ENVS=1 KEEP_RAW=1

Endpoint tiers:
  read   — plaintext, json, params
  upload — validate, post-form, post-json-1k, post-octet-64k, post-octet-2m,
           post-stream-2m, multipart-2m
  all    — every endpoint (default)

Large upload cases use UPLOAD_CONNECTIONS (default 32) instead of CONNECTIONS.

PORT is the base port. Targets use fixed offsets in TARGETS order (stario=PORT,
stario-cython=PORT+1, socketify=PORT+2, ...).
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
    post-form) echo /form ;;
    post-json-1k) echo /echo/json ;;
    post-octet-64k) echo /ingest/64k ;;
    post-octet-2m) echo /ingest/2m ;;
    post-stream-2m) echo /ingest/stream/2m ;;
    multipart-2m) echo /upload ;;
    *)
      echo "Unknown endpoint: $1" >&2
      return 1
      ;;
  esac
}

script_for() {
  case "$1" in
    validate) echo "$BENCHMARK_DIR/validate.lua" ;;
    post-form) echo "$BENCHMARK_DIR/scripts/post-form.lua" ;;
    post-json-1k) echo "$BENCHMARK_DIR/scripts/post-json-1k.lua" ;;
    post-octet-64k) echo "$BENCHMARK_DIR/scripts/post-octet-64k.lua" ;;
    post-octet-2m) echo "$BENCHMARK_DIR/scripts/post-octet-2m.lua" ;;
    post-stream-2m) echo "$BENCHMARK_DIR/scripts/post-stream-2m.lua" ;;
    multipart-2m) echo "$BENCHMARK_DIR/scripts/multipart-2m.lua" ;;
    *) echo "" ;;
  esac
}

endpoint_label() {
  local endpoint="$1" entry label
  for entry in "${ENDPOINT_LABELS[@]}"; do
    label="${entry#*|}"
    if [[ "${entry%%|*}" == "$endpoint" ]]; then
      echo "$label"
      return 0
    fi
  done
  echo "$endpoint"
}

endpoint_selected() {
  local endpoint="$1" item
  for item in "${ENDPOINTS[@]}"; do
    [[ "$item" == "$endpoint" ]] && return 0
  done
  return 1
}

connections_for() {
  case "$1" in
    post-octet-64k|post-octet-2m|post-stream-2m|multipart-2m)
      echo "$UPLOAD_CONNECTIONS"
      ;;
    *)
      echo "$CONNECTIONS"
      ;;
  esac
}

parse_endpoints() {
  if [[ -n "${ENDPOINTS_CSV:-}" ]]; then
    IFS=',' read -ra ENDPOINTS <<< "$ENDPOINTS_CSV"
    return
  fi
  if [[ -n "${ENDPOINT_TIER:-}" ]]; then
    case "$ENDPOINT_TIER" in
      read|core) ENDPOINTS=("${READ_ENDPOINTS[@]}") ;;
      upload) ENDPOINTS=("${UPLOAD_ENDPOINTS[@]}") ;;
      all) ENDPOINTS=("${DEFAULT_ENDPOINTS[@]}") ;;
      *)
        echo "Unknown ENDPOINT_TIER: $ENDPOINT_TIER (use read, upload, or all)" >&2
        exit 1
        ;;
    esac
    return
  fi
  ENDPOINTS=("${DEFAULT_ENDPOINTS[@]}")
}

ensure_fixtures() {
  local python="$1"
  "$python" "$BENCHMARK_DIR/fixtures/generate.py"
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

host_python() {
  if [[ -n "${STATS_PYTHON:-}" ]]; then
    echo "$STATS_PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  echo "python3"
}

env_name_for() {
  case "$1" in
    stario-n) echo stario ;;
    stario-cython-n) echo stario-cython ;;
    granian-rsgi-n) echo granian-rsgi ;;
    *) echo "$1" ;;
  esac
}

python_for() {
  local candidate="$ENVS_DIR/$(env_name_for "$1")/bin/python"
  if [[ -x "$candidate" ]]; then
    echo "$candidate"
  else
    host_python
  fi
}

workers_for() {
  case "$1" in
    stario-n|stario-cython-n) echo "$BENCH_PROCS" ;;
    *) echo 1 ;;
  esac
}

needs_uv() {
  local target
  for target in "$@"; do
    case "$target" in
      go-nethttp|go-nethttp-n|go-fasthttp) ;;
      *) return 0 ;;
    esac
  done
  return 1
}

needs_go() {
  local target
  for target in "$@"; do
    case "$target" in
      go-nethttp|go-nethttp-n|go-fasthttp) return 0 ;;
    esac
  done
  return 1
}

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
  local name="$1"
  local python="$ENVS_DIR/$name/bin/python"
  shift
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

target_label() {
  local target="$1" entry label
  for entry in "${TARGET_LABELS[@]}"; do
    label="${entry#*|}"
    if [[ "${entry%%|*}" == "$target" ]]; then
      echo "$label"
      return 0
    fi
  done
  echo "$target"
}

ensure_target() {
  case "$1" in
    stario|stario-n) ensure_env stario "stario @ file://$ROOT" uvloop ujson ;;
    stario-cython|stario-cython-n)
      ensure_env stario-cython "stario @ file://$ROOT" uvloop ujson cython setuptools wheel
      build_stario_cython "$(python_for stario-cython)"
      ;;
    socketify) ensure_env socketify socketify ujson ;;
    robyn) ensure_env robyn robyn ujson ;;
    granian-rsgi) ensure_env granian-rsgi granian uvloop ujson ;;
    sanic) ensure_env sanic sanic uvloop ujson ;;
    django-bolt) ensure_env django-bolt django django-bolt msgspec ujson ;;
    fastapi) ensure_env fastapi fastapi 'uvicorn[standard]' ujson ;;
    blacksheep-uvicorn) ensure_env blacksheep-uvicorn blacksheep 'uvicorn[standard]' ujson ;;
    blacksheep-granian) ensure_env blacksheep-granian blacksheep granian uvloop ujson ;;
    granian-rsgi-n) ensure_env granian-rsgi granian uvloop ujson ;;
    go-nethttp|go-nethttp-n|go-fasthttp) build_go ;;
  esac
}

build_go() {
  local bin="$BENCHMARK_DIR/.bin/stario-go-bench"
  if [[ ! -x "$bin" || "${REFRESH_ENVS:-0}" == 1 ]]; then
    echo "  building Go benchmark servers"
    mkdir -p "$BENCHMARK_DIR/.bin"
    (cd "$BENCHMARK_DIR/apps/go" && go build -o "$bin" .)
  fi
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
    stario|stario-cython|stario-n|stario-cython-n)
      export STARIO_HOST="$HOST"
      export STARIO_PORT="$SERVER_PORT"
      export STARIO_LOOP=uvloop
      export STARIO_TRACER=noop
      export STARIO_COMPRESS_ZSTD_LEVEL=-1
      export STARIO_COMPRESS_BROTLI_LEVEL=-1
      export STARIO_COMPRESS_GZIP_LEVEL=-1
      case "$1" in
        stario-n|stario-cython-n) export STARIO_REUSE_PORT=1 ;;
        *) export STARIO_REUSE_PORT=0 ;;
      esac
      if [[ "$1" == stario-cython || "$1" == stario-cython-n ]]; then
        SERVER_CMD=(
          env PYTHONPATH="$ROOT/src:$BENCHMARK_DIR"
          STARIO_REUSE_PORT="$STARIO_REUSE_PORT"
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
    socketify)
      export BENCH_HOST="$HOST"
      export BENCH_PORT="$SERVER_PORT"
      SERVER_CMD=(
        "$(python_for socketify)" "$BENCHMARK_DIR/apps/socketify_app.py"
      )
      ;;
    robyn)
      export BENCH_HOST="$HOST"
      export BENCH_PORT="$SERVER_PORT"
      SERVER_CMD=(
        "$(python_for robyn)" "$BENCHMARK_DIR/apps/robyn_app.py"
        --log-level=WARN --disable-openapi --processes 1 --workers 1
      )
      ;;
    granian-rsgi)
      SERVER_CMD=(
        "$(python_for granian-rsgi)" -m granian apps.granian_rsgi_app:app
        --interface rsgi
        --host "$HOST" --port "$SERVER_PORT"
        --workers 1
        --runtime-threads 1
        --loop uvloop
        --no-access-log
        --log-level warning
      )
      ;;
    granian-rsgi-n)
      SERVER_CMD=(
        "$(python_for granian-rsgi)" -m granian apps.granian_rsgi_app:app
        --interface rsgi
        --host "$HOST" --port "$SERVER_PORT"
        --workers "$BENCH_PROCS"
        --runtime-threads 1
        --loop uvloop
        --no-access-log
        --log-level warning
      )
      ;;
    go-nethttp|go-nethttp-n|go-fasthttp)
      local gomax=1 impl=nethttp
      [[ "$1" == go-nethttp-n ]] && gomax="$BENCH_PROCS"
      [[ "$1" == go-fasthttp ]] && impl=fasthttp
      SERVER_CMD=(
        env GOMAXPROCS="$gomax"
        BENCH_HOST="$HOST" BENCH_PORT="$SERVER_PORT"
        "$BENCHMARK_DIR/.bin/stario-go-bench" -impl "$impl"
        -host "$HOST" -port "$SERVER_PORT"
      )
      ;;
    django-bolt)
      SERVER_CMD=(
        "$(python_for django-bolt)" "$BENCHMARK_DIR/apps/django_bolt/manage.py"
        runbolt --host "$HOST" --port "$SERVER_PORT" --processes 1
      )
      ;;
  esac
}

stop_server() {
  local pid deadline=$((SECONDS + 5))
  if ((${#SERVER_PIDS[@]})); then
    for pid in "${SERVER_PIDS[@]}"; do
      if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
        kill "$pid" >/dev/null 2>&1 || true
      fi
    done
    for pid in "${SERVER_PIDS[@]}"; do
      [[ -n "$pid" ]] || continue
      while kill -0 "$pid" >/dev/null 2>&1; do
        if ((SECONDS >= deadline)); then
          kill -9 "$pid" >/dev/null 2>&1 || true
          break
        fi
        sleep 0.1
      done
      wait "$pid" >/dev/null 2>&1 || true
    done
  fi
  SERVER_PID=""
  SERVER_PIDS=()
}

workers_alive() {
  local pid
  ((${#SERVER_PIDS[@]})) || return 1
  for pid in "${SERVER_PIDS[@]}"; do
    if [[ -n "$pid" ]] && ! kill -0 "$pid" >/dev/null 2>&1; then
      return 1
    fi
  done
  return 0
}

fail_with_log() {
  local message="$1" log="$2"
  echo "$message Log: $log" >&2
  sed -n '1,120p' "$log" >&2 || true
  exit 1
}

wait_ready() {
  local url="http://$HOST:$SERVER_PORT/plaintext" deadline=$((SECONDS + 30)) log="$1"
  until curl -fsS --max-time 1 "$url" >/dev/null 2>&1; do
    if ! workers_alive; then
      fail_with_log "Server exited before it was ready." "$log"
    fi
    if ((SECONDS >= deadline)); then
      fail_with_log "Server did not become ready at $url." "$log"
    fi
    sleep 0.1
  done
  if ! workers_alive; then
    fail_with_log "Server exited after readiness check." "$log"
  fi
}

start_server() {
  local target="$1" log="$RUN_DIR/$target.server.log" workers i
  command_for "$target"
  workers="$(workers_for "$target")"
  echo "+ ${SERVER_CMD[*]}"
  if ((workers > 1)); then
    echo "  $workers processes on $HOST:$SERVER_PORT (STARIO_REUSE_PORT=1)"
  fi
  SERVER_PIDS=()
  : >"$log"
  for ((i = 0; i < workers; i++)); do
    (cd "$ROOT" && exec env PYTHONPATH="$ROOT/src:$BENCHMARK_DIR" "${SERVER_CMD[@]}") >>"$log" 2>&1 &
    SERVER_PIDS+=("$!")
  done
  SERVER_PID="${SERVER_PIDS[0]}"
  wait_ready "$log"
}

aggregate_samples() {
  local stats_file="$1" python="$2"
  "$python" "$BENCHMARK_DIR/stats.py" >"$stats_file"
}

run_endpoint() {
  local target="$1" endpoint="$2" venv_python samples_file stats_file sample raw_out run_idx total script connections
  local url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
  local cmd=("$WRK" -t "$THREADS" -c "$(connections_for "$endpoint")" -d "$DURATION")
  script="$(script_for "$endpoint")"
  [[ -z "$script" || -f "$script" ]] || { echo "Missing wrk script for $endpoint: $script" >&2; exit 1; }
  [[ -n "$script" ]] && cmd+=(-s "$script")

  samples_file="$RUN_DIR/$target.$endpoint.samples"
  stats_file="$RUN_DIR/$target.$endpoint.stats.json"
  : >"$samples_file"
  venv_python="$(python_for "$target")"
  connections="$(connections_for "$endpoint")"

  total=$((RUNS + WARMUP))
  echo "  $endpoint ($(endpoint_label "$endpoint"), ${connections} conn, $RUNS measured + $WARMUP warmup)"
  for ((run_idx = 1; run_idx <= total; run_idx++)); do
    raw_out="$RUN_DIR/$target.$endpoint.run${run_idx}.txt"
    (cd "$ROOT" && "${cmd[@]}" "$url") >"$raw_out"
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

print_summary_row() {
  local target="$1" venv_python="$2" section_endpoints=("${@:3}")
  local endpoint stats_file cell
  printf '| %s ' "$(target_label "$target")"
  for endpoint in "${section_endpoints[@]}"; do
    endpoint_selected "$endpoint" || continue
    stats_file="$RUN_DIR/$target.$endpoint.stats.json"
    if [[ -f "$stats_file" ]]; then
      cell="$(format_cell "$stats_file" "$venv_python")"
      printf '| %s ' "$cell"
    else
      printf '| - '
    fi
  done
  echo "|"
}

print_section_table() {
  local section_name="$1" section_endpoints=("${@:2}")
  local endpoint printed=0 label header=()
  for endpoint in "${section_endpoints[@]}"; do
    endpoint_selected "$endpoint" || continue
    printed=1
    header+=("$(endpoint_label "$endpoint")")
  done
  [[ "$printed" == 1 ]] || return 0
  echo
  echo "### $section_name"
  echo
  printf '| Server '
  for label in "${header[@]}"; do
    printf '| %s ' "$label"
  done
  echo "|"
  printf '| --- '
  for _label in "${header[@]}"; do
    printf '| ---: '
  done
  echo "|"
}

print_summary_section() {
  local group_name="$1" group_printed_ref="$2" group_targets="$3"
  local section_name="$4"
  shift 4
  local section_endpoints=("$@")
  local has_rows=0 target_in_group endpoint
  for target_in_group in $group_targets; do
    target_selected "$target_in_group" || continue
    for endpoint in "${section_endpoints[@]}"; do
      endpoint_selected "$endpoint" || continue
      [[ -f "$RUN_DIR/$target_in_group.$endpoint.stats.json" ]] && has_rows=1
    done
  done
  [[ "$has_rows" == 1 ]] || return 0
  if [[ "${!group_printed_ref}" == 0 ]]; then
    echo
    echo "## $group_name"
    printf -v "$group_printed_ref" 1
  fi
  print_section_table "$section_name" "${section_endpoints[@]}"
  for target_in_group in $group_targets; do
    target_selected "$target_in_group" || continue
    print_summary_row "$target_in_group" "$(python_for "$target_in_group")" "${section_endpoints[@]}"
  done
}

print_summary() {
  local group_entry group_name group_targets table="$RUN_DIR/summary.md"
  local printed_any=0 group_printed=0
  SELECTED_TARGETS=("$@")
  : >"$table"
  {
    echo "## HTTP server comparison"
    echo
    echo "Median requests/sec ± sample stdev after discarding $WARMUP warmup run(s)"
    echo "and applying IQR outlier trimming across $RUNS measured samples per endpoint."
    echo "Large uploads use $UPLOAD_CONNECTIONS connections; other cases use $CONNECTIONS."
    echo "Go \`*-n\` uses BENCH_PROCS=$BENCH_PROCS in one process; stario-n / stario-cython-n start $BENCH_PROCS processes with STARIO_REUSE_PORT=1; granian-rsgi-n uses $BENCH_PROCS workers. Other rows stay 1 worker / GOMAXPROCS=1."
    for group_entry in "${SUMMARY_GROUPS[@]}"; do
      group_name="${group_entry%%|*}"
      group_targets="${group_entry#*|}"
      group_printed=0
      print_summary_section "$group_name" group_printed "$group_targets" "Read-heavy" "${READ_ENDPOINTS[@]}"
      print_summary_section "$group_name" group_printed "$group_targets" "Upload / body" "${UPLOAD_ENDPOINTS[@]}"
      if [[ "$group_printed" == 1 ]]; then
        printed_any=1
      fi
    done
    if [[ "$printed_any" == 0 ]]; then
      echo
      echo "_No results recorded._"
    fi
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

target_selected() {
  local target="$1" selected
  for selected in "${SELECTED_TARGETS[@]}"; do
    [[ "$selected" == "$target" ]] && return 0
  done
  return 1
}

main() {
  need curl
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Missing required command: python3 (used for fixture generation and wrk stats)" >&2
    exit 1
  fi
  if ! command -v "$WRK" >/dev/null 2>&1; then
    echo "Missing wrk executable: $WRK" >&2
    echo "Install wrk or set WRK=/path/to/wrk." >&2
    exit 1
  fi
  parse_endpoints
  trap stop_server EXIT INT TERM

  local selected=("$@") target endpoint
  if ((${#selected[@]} == 0)); then selected=("${TARGETS[@]}"); fi
  case "${selected[0]}" in -h|--help|help) usage; exit 0 ;; esac
  for target in "${selected[@]}"; do known_target "$target" || { echo "Unknown target: $target" >&2; usage >&2; exit 1; }; done
  for endpoint in "${ENDPOINTS[@]}"; do path_for "$endpoint" >/dev/null || exit 1; done
  if needs_uv "${selected[@]}"; then
    need uv
  fi
  if needs_go "${selected[@]}"; then
    need go
  fi

  ensure_fixtures "$(host_python)"

  RUN_DIR="$RESULTS_DIR/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$RUN_DIR"
  cat >"$RUN_DIR/config.txt" <<EOF
host=$HOST
base_port=$PORT
duration=$DURATION
threads=$THREADS
connections=$CONNECTIONS
upload_connections=$UPLOAD_CONNECTIONS
runs=$RUNS
warmup=$WARMUP
python=$PYTHON
bench_procs=$BENCH_PROCS
client=$WRK
keep_raw=$KEEP_RAW
endpoints=${ENDPOINTS[*]}
git_sha=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
git_branch=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
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
