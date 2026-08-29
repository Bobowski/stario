#!/usr/bin/env bash
# Compare main vs cython-core on uvloop vs zuvloop across the official
# Stario HTTP suite (read + upload) from cython-core.
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$HERE/../.." && pwd)"
MAIN_ROOT="${MAIN_ROOT:-$WORKSPACE}"
CYTHON_ROOT="${CYTHON_ROOT:-/tmp/stario-cython}"
BENCH_DIR="${BENCH_DIR:-$CYTHON_ROOT/benchmarks/server}"
ENVS_DIR="${ENVS_DIR:-$HERE/.venvs}"
RESULTS_DIR="${RESULTS_DIR:-$HERE/results}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3100}"
DURATION="${DURATION:-10s}"
THREADS="${THREADS:-2}"
CONNECTIONS="${CONNECTIONS:-128}"
UPLOAD_CONNECTIONS="${UPLOAD_CONNECTIONS:-32}"
RUNS="${RUNS:-5}"
WARMUP="${WARMUP:-1}"
PYTHON="${PYTHON:-3.14}"
KEEP_RAW="${KEEP_RAW:-1}"
WRK="${WRK:-wrk}"

READ_ENDPOINTS=(plaintext json params)
UPLOAD_ENDPOINTS=(validate post-form post-json-1k post-octet-64k post-octet-2m post-stream-2m multipart-2m)
ENDPOINTS=("${READ_ENDPOINTS[@]}" "${UPLOAD_ENDPOINTS[@]}")

# name|label|tree|protocol|loop
TARGETS=(
  "main-uvloop|main Python httptools + uvloop|main|python|uvloop"
  "main-zuvloop|main Python httptools + zuvloop|main|python|zuvloop"
  "cython-core-uvloop|cython-core Python httptools + uvloop|cython|python|uvloop"
  "cython-core-zuvloop|cython-core Python httptools + zuvloop|cython|python|zuvloop"
  "cython-llhttp-uvloop|cython-core Cython llhttp + uvloop|cython|cython|uvloop"
  "cython-llhttp-zuvloop|cython-core Cython llhttp + zuvloop|cython|cython|zuvloop"
)

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
SERVER_PORT=""

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}

split_target() {
  local spec="$1"
  IFS='|' read -r TARGET_NAME TARGET_LABEL TARGET_TREE TARGET_PROTOCOL TARGET_LOOP <<<"$spec"
}

root_for() {
  case "$1" in
    main) echo "$MAIN_ROOT" ;;
    cython) echo "$CYTHON_ROOT" ;;
    *) echo "Unknown tree: $1" >&2; return 1 ;;
  esac
}

python_for_tree() {
  echo "$ENVS_DIR/$1/bin/python"
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
  esac
}

script_for() {
  case "$1" in
    validate) echo "$BENCH_DIR/validate.lua" ;;
    post-form) echo "$BENCH_DIR/scripts/post-form.lua" ;;
    post-json-1k) echo "$BENCH_DIR/scripts/post-json-1k.lua" ;;
    post-octet-64k) echo "$BENCH_DIR/scripts/post-octet-64k.lua" ;;
    post-octet-2m) echo "$BENCH_DIR/scripts/post-octet-2m.lua" ;;
    post-stream-2m) echo "$BENCH_DIR/scripts/post-stream-2m.lua" ;;
    multipart-2m) echo "$BENCH_DIR/scripts/multipart-2m.lua" ;;
    *) echo "" ;;
  esac
}

endpoint_label() {
  local endpoint="$1" entry
  for entry in "${ENDPOINT_LABELS[@]}"; do
    if [[ "${entry%%|*}" == "$endpoint" ]]; then
      echo "${entry#*|}"
      return 0
    fi
  done
  echo "$endpoint"
}

connections_for() {
  case "$1" in
    post-octet-64k|post-octet-2m|post-stream-2m|multipart-2m) echo "$UPLOAD_CONNECTIONS" ;;
    *) echo "$CONNECTIONS" ;;
  esac
}

target_offset() {
  local index=0 spec
  for spec in "${TARGETS[@]}"; do
    split_target "$spec"
    if [[ "$TARGET_NAME" == "$1" ]]; then
      echo "$index"
      return 0
    fi
    index=$((index + 1))
  done
  return 1
}

port_for() {
  echo $((PORT + $(target_offset "$1")))
}

ensure_venv() {
  local tree="$1" root python
  root="$(root_for "$tree")"
  python="$(python_for_tree "$tree")"
  if [[ ! -x "$python" ]]; then
    echo "Creating venv for $tree at $ENVS_DIR/$tree"
    uv venv "$ENVS_DIR/$tree" --python "$PYTHON"
    uv pip install --python "$python" "stario @ file://$root" uvloop zuvloop ujson
    if [[ "$tree" == cython ]]; then
      uv pip install --python "$python" cython setuptools wheel
      echo "Building stario_cython extensions"
      (cd "$root" && "$python" setup.py)
    fi
  fi
}

require_port_free() {
  local python="$1" port="$2"
  if ! "$python" - "$HOST" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
family = socket.AF_INET6 if ":" in host else socket.AF_INET
with socket.socket(family, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
PY
  then
    echo "Port $HOST:$port is already in use." >&2
    exit 1
  fi
}

stop_server() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    local deadline=$((SECONDS + 5))
    while kill -0 "$SERVER_PID" >/dev/null 2>&1; do
      if ((SECONDS >= deadline)); then
        kill -9 "$SERVER_PID" >/dev/null 2>&1 || true
        break
      fi
      sleep 0.1
    done
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  SERVER_PID=""
}

fail_with_log() {
  echo "$1 Log: $2" >&2
  sed -n '1,160p' "$2" >&2 || true
  exit 1
}

wait_ready() {
  local url="http://$HOST:$SERVER_PORT/plaintext" deadline=$((SECONDS + 30)) log="$1"
  until curl -fsS --max-time 1 "$url" >/dev/null 2>&1; do
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      fail_with_log "Server exited before it was ready." "$log"
    fi
    if ((SECONDS >= deadline)); then
      fail_with_log "Server did not become ready at $url." "$log"
    fi
    sleep 0.1
  done
}

start_server() {
  local spec="$1" log root python
  split_target "$spec"
  root="$(root_for "$TARGET_TREE")"
  python="$(python_for_tree "$TARGET_TREE")"
  log="$RUN_DIR/$TARGET_NAME.server.log"

  export STARIO_HOST="$HOST"
  export STARIO_PORT="$SERVER_PORT"
  export STARIO_LOOP="$TARGET_LOOP"
  export STARIO_TRACER=noop
  export STARIO_COMPRESS_ZSTD_LEVEL=-1
  export STARIO_COMPRESS_BROTLI_LEVEL=-1
  export STARIO_COMPRESS_GZIP_LEVEL=-1

  local pythonpath="$BENCH_DIR"
  # cython-core Headers live in the compiled extension; src must be importable.
  if [[ "$TARGET_TREE" == cython ]]; then
    pythonpath="$root/src:$BENCH_DIR"
  fi
  local cmd=()
  if [[ "$TARGET_PROTOCOL" == cython ]]; then
    cmd=(env "PYTHONPATH=$pythonpath" "$python" -m stario_cython apps.stario_app:bootstrap)
  else
    cmd=(env "PYTHONPATH=$pythonpath" "$python" -m stario.cli serve apps.stario_app:bootstrap)
  fi

  echo "+ STARIO_LOOP=$TARGET_LOOP ${cmd[*]}"
  (cd "$root" && exec "${cmd[@]}") >"$log" 2>&1 &
  SERVER_PID="$!"
  wait_ready "$log"
}

aggregate_samples() {
  local stats_file="$1" python="$2"
  "$python" "$BENCH_DIR/stats.py" >"$stats_file"
}

run_endpoint() {
  local name="$1" endpoint="$2" python="$3"
  local url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
  local connections script samples_file stats_file raw_out sample run_idx total
  connections="$(connections_for "$endpoint")"
  script="$(script_for "$endpoint")"
  samples_file="$RUN_DIR/$name.$endpoint.samples"
  stats_file="$RUN_DIR/$name.$endpoint.stats.json"
  : >"$samples_file"

  local cmd=("$WRK" -t "$THREADS" -c "$connections" -d "$DURATION")
  [[ -n "$script" ]] && cmd+=(-s "$script")

  total=$((RUNS + WARMUP))
  echo "  $endpoint ($(endpoint_label "$endpoint"), ${connections} conn, $RUNS measured + $WARMUP warmup)"
  for ((run_idx = 1; run_idx <= total; run_idx++)); do
    raw_out="$RUN_DIR/$name.$endpoint.run${run_idx}.txt"
    (cd "$CYTHON_ROOT" && "${cmd[@]}" "$url") >"$raw_out"
    sample="$(awk '/Requests\/sec:/ {print $2; exit}' "$raw_out")"
    [[ -n "$sample" ]] || { echo "Failed to parse wrk output: $raw_out" >&2; exit 1; }
    if ((run_idx > WARMUP)); then
      echo "$sample" >>"$samples_file"
    fi
    if [[ "$KEEP_RAW" != 1 ]]; then
      rm -f "$raw_out"
    fi
  done
  aggregate_samples "$stats_file" "$python" <"$samples_file"
}

run_target() {
  local spec="$1" endpoint
  split_target "$spec"
  echo
  echo "== $TARGET_NAME ($TARGET_LABEL) =="
  ensure_venv "$TARGET_TREE"
  SERVER_PORT="$(port_for "$TARGET_NAME")"
  require_port_free "$(python_for_tree "$TARGET_TREE")" "$SERVER_PORT"
  echo "  port $SERVER_PORT loop=$TARGET_LOOP protocol=$TARGET_PROTOCOL"
  start_server "$spec"
  for endpoint in "${ENDPOINTS[@]}"; do
    run_endpoint "$TARGET_NAME" "$endpoint" "$(python_for_tree "$TARGET_TREE")"
  done
  stop_server
}

format_cell() {
  local stats_file="$1" python="$2"
  "$python" - "$stats_file" <<'PY'
import json, pathlib, sys
stats = json.loads(pathlib.Path(sys.argv[1]).read_text())

def fmt(value: float) -> str:
    return f"{value:,.0f}"

cell = f"{fmt(stats['median'])} ± {fmt(stats['stdev'])}"
if stats["outliers_removed"]:
    n = stats["outliers_removed"]
    cell += f" ({n} outlier{'s' if n != 1 else ''} removed)"
print(cell)
PY
}

print_summary() {
  local table="$RUN_DIR/summary.md" python spec endpoint stats_file
  python="$(python_for_tree main)"
  : >"$table"
  {
    echo "## main vs cython-core × uvloop vs zuvloop"
    echo
    echo "Median requests/sec ± sample stdev after discarding $WARMUP warmup run(s)"
    echo "and applying IQR outlier trimming across $RUNS measured samples per endpoint."
    echo "Large uploads use $UPLOAD_CONNECTIONS connections; other cases use $CONNECTIONS."
    echo
    echo "### Read-heavy"
    echo
    printf '| Server '
    for endpoint in "${READ_ENDPOINTS[@]}"; do printf '| %s ' "$(endpoint_label "$endpoint")"; done
    echo "|"
    printf '| --- '
    for _ in "${READ_ENDPOINTS[@]}"; do printf '| ---: '; done
    echo "|"
    for spec in "${TARGETS[@]}"; do
      split_target "$spec"
      printf '| %s ' "$TARGET_LABEL"
      for endpoint in "${READ_ENDPOINTS[@]}"; do
        stats_file="$RUN_DIR/$TARGET_NAME.$endpoint.stats.json"
        if [[ -f "$stats_file" ]]; then
          printf '| %s ' "$(format_cell "$stats_file" "$python")"
        else
          printf '| - '
        fi
      done
      echo "|"
    done
    echo
    echo "### Upload / body"
    echo
    printf '| Server '
    for endpoint in "${UPLOAD_ENDPOINTS[@]}"; do printf '| %s ' "$(endpoint_label "$endpoint")"; done
    echo "|"
    printf '| --- '
    for _ in "${UPLOAD_ENDPOINTS[@]}"; do printf '| ---: '; done
    echo "|"
    for spec in "${TARGETS[@]}"; do
      split_target "$spec"
      printf '| %s ' "$TARGET_LABEL"
      for endpoint in "${UPLOAD_ENDPOINTS[@]}"; do
        stats_file="$RUN_DIR/$TARGET_NAME.$endpoint.stats.json"
        if [[ -f "$stats_file" ]]; then
          printf '| %s ' "$(format_cell "$stats_file" "$python")"
        else
          printf '| - '
        fi
      done
      echo "|"
    done
  } | tee "$table"
}

ensure_fixtures() {
  local python
  ensure_venv main
  python="$(python_for_tree main)"
  "$python" "$BENCH_DIR/fixtures/generate.py"
}

main() {
  need uv; need curl; need "$WRK"
  [[ -d "$CYTHON_ROOT" ]] || { echo "Missing cython-core worktree: $CYTHON_ROOT" >&2; exit 1; }
  [[ -d "$BENCH_DIR" ]] || { echo "Missing bench dir: $BENCH_DIR" >&2; exit 1; }
  trap stop_server EXIT INT TERM

  ensure_fixtures
  ensure_venv cython

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
client=$WRK
keep_raw=$KEEP_RAW
endpoints=${ENDPOINTS[*]}
main_root=$MAIN_ROOT
cython_root=$CYTHON_ROOT
main_sha=$(git -C "$MAIN_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
cython_sha=$(git -C "$CYTHON_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
zuvloop=$("$ENVS_DIR/main/bin/python" -c "import zuvloop,importlib.metadata as m; print(getattr(zuvloop,'__version__', m.version('zuvloop')))")
uvloop=$("$ENVS_DIR/main/bin/python" -c "import uvloop,importlib.metadata as m; print(getattr(uvloop,'__version__', m.version('uvloop')))")
EOF

  echo "Writing results to $RUN_DIR"
  local spec
  for spec in "${TARGETS[@]}"; do
    run_target "$spec"
  done
  echo
  print_summary
  echo
  echo "Summary: $RUN_DIR/summary.md"
}

main "$@"
