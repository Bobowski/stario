#!/usr/bin/env bash
# Stario-only HTTP suite across asyncio-compatible event loops.
set -euo pipefail

export PATH="${HOME}/.local/bin:${PATH}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCHMARK_DIR="$ROOT/benchmarks/server"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVS_DIR="$HERE/.venvs"
RESULTS_DIR="$HERE/results"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3200}"
DURATION="${DURATION:-10s}"
THREADS="${THREADS:-2}"
CONNECTIONS="${CONNECTIONS:-128}"
RUNS="${RUNS:-3}"
WARMUP="${WARMUP:-1}"
PYTHON="${PYTHON:-3.14}"
KEEP_RAW="${KEEP_RAW:-0}"
WRK="${WRK:-wrk}"

ENV_NAME=stario-loops
ALL_LOOPS=(asyncio uvloop zuvloop rloop rsloop uringcore)
LOOP_PACKAGES=(uvloop zuvloop rloop rsloop uringcore)
ENDPOINTS=(plaintext json params validate)

RUN_DIR=""
SERVER_PID=""
SERVER_PORT=""
SELECTED_LOOPS=()

usage() {
  cat <<'EOF'
Usage: benchmarks/loops/run.sh [asyncio|uvloop|zuvloop|rloop|rsloop|uringcore ...]

Environment: DURATION=10s RUNS=3 WARMUP=1 THREADS=2 CONNECTIONS=128
             HOST=127.0.0.1 PORT=3200 PYTHON=3.14 REFRESH_ENVS=1 KEEP_RAW=1
EOF
}

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}

python_bin() { echo "$ENVS_DIR/$ENV_NAME/bin/python"; }

path_for() {
  case "$1" in
    plaintext) echo /plaintext ;;
    json) echo /json ;;
    params) echo /user/42 ;;
    validate) echo /validate ;;
  esac
}

format_int() {
  local value="$1" out=""
  while ((${#value} > 3)); do
    out=",${value:${#value}-3:3}$out"
    value="${value:0:${#value}-3}"
  done
  printf '%s%s' "$value" "$out"
}

loop_offset() {
  local index=0 name
  for name in "${ALL_LOOPS[@]}"; do
    if [[ "$name" == "$1" ]]; then
      echo "$index"
      return 0
    fi
    index=$((index + 1))
  done
  return 1
}

port_for() {
  echo $((PORT + $(loop_offset "$1")))
}

known_loop() {
  loop_offset "$1" >/dev/null
}

require_port_free() {
  local python="$1" port="$2"
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
    echo "Port $HOST:$port is already in use." >&2
    exit 1
  fi
}

ensure_env() {
  local python
  python="$(python_bin)"
  [[ "${REFRESH_ENVS:-0}" == 1 ]] && rm -rf "$ENVS_DIR/$ENV_NAME"
  if [[ ! -x "$python" ]]; then
    uv venv "$ENVS_DIR/$ENV_NAME" --python "$PYTHON"
    uv pip install --python "$python" "stario @ file://$ROOT" ujson
  fi
  local pkg
  for pkg in "${LOOP_PACKAGES[@]}"; do
    if ! "$python" -c "import $pkg" >/dev/null 2>&1; then
      uv pip install --python "$python" "$pkg" && continue
      echo "WARN: could not install $pkg; that loop will be skipped." >&2
    fi
  done
}

loop_available() {
  local loop="$1" python
  python="$(python_bin)"
  if [[ "$loop" == asyncio ]]; then
    return 0
  fi
  "$python" -c "import $loop" >/dev/null 2>&1
}

pkg_version() {
  local name="$1" python
  python="$(python_bin)"
  if [[ "$name" == asyncio ]]; then
    "$python" -c "import asyncio; print(getattr(asyncio, '__version__', 'stdlib'))"
    return
  fi
  "$python" - <<PY
import importlib.metadata as m
try:
    print(m.version("$name"))
except m.PackageNotFoundError:
    print("missing")
PY
}

write_machine_info() {
  local python out="$1"
  python="$(python_bin)"
  {
    echo "=== machine ==="
    hostnamectl 2>/dev/null || true
    echo "hostname=$(hostname)"
    echo "uname=$(uname -a)"
    echo "os=$({ . /etc/os-release && echo "$PRETTY_NAME"; } 2>/dev/null || uname -s)"
    echo "kernel=$(uname -r)"
    echo "arch=$(uname -m)"
    echo "cpu_model=$(awk -F: '/model name/ {gsub(/^ /,"",$2); print $2; exit}' /proc/cpuinfo)"
    echo "cpu_count=$(nproc)"
    echo "mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
    lscpu 2>/dev/null | awk '/^Architecture|^CPU\(s\)|^Model name|^Thread|^Core|^Socket|^Hypervisor|^Virtualization|^Flags/{print}'
    echo
    echo "=== python ==="
    "$python" - <<'PY'
import platform, sys
print(f"executable={sys.executable}")
print(f"version={sys.version.replace(chr(10), ' ')}")
print(f"implementation={platform.python_implementation()}")
print(f"platform={platform.platform()}")
PY
  } >>"$out"
}

stop_server() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    local deadline=$((SECONDS + 5))
    while kill -0 "$SERVER_PID" >/dev/null 2>&1 && ((SECONDS < deadline)); do
      sleep 0.1
    done
    if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      kill -9 "$SERVER_PID" >/dev/null 2>&1 || true
    fi
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  SERVER_PID=""
}

fail_log() {
  echo "$1 Log: $2" >&2
  sed -n '1,160p' "$2" >&2 || true
}

wait_ready() {
  local url="http://$HOST:$SERVER_PORT/plaintext" deadline=$((SECONDS + 20)) log="$1"
  until curl -fsS --max-time 1 "$url" >/dev/null 2>&1; do
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      fail_log "Server exited before it was ready." "$log"
      return 1
    fi
    if ((SECONDS >= deadline)); then
      fail_log "Server did not become ready at $url." "$log"
      return 1
    fi
    sleep 0.1
  done
  return 0
}

start_server() {
  local loop="$1" log="$RUN_DIR/$loop.server.log" python
  python="$(python_bin)"
  export STARIO_HOST="$HOST"
  export STARIO_PORT="$SERVER_PORT"
  export STARIO_LOOP="$loop"
  export STARIO_TRACER=noop
  export STARIO_COMPRESS_ZSTD_LEVEL=-1
  export STARIO_COMPRESS_BROTLI_LEVEL=-1
  export STARIO_COMPRESS_GZIP_LEVEL=-1
  echo "+ STARIO_LOOP=$loop $python -m stario.cli serve apps.stario_app:bootstrap"
  (cd "$ROOT" && exec env PYTHONPATH="$BENCHMARK_DIR" "$python" -m stario.cli serve apps.stario_app:bootstrap) >"$log" 2>&1 &
  SERVER_PID="$!"
  wait_ready "$log"
}

aggregate_samples() {
  local samples_file="$1" stats_file="$2" python
  python="$(python_bin)"
  "$python" - "$samples_file" "$stats_file" <<'PY'
import json, pathlib, statistics, sys

samples_path, stats_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
samples = [float(line) for line in samples_path.read_text().splitlines() if line.strip()]
if not samples:
    raise SystemExit(f"no samples in {samples_path}")
stats = {
    "n": len(samples),
    "median": statistics.median(samples),
    "mean": statistics.fmean(samples),
    "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
    "min": min(samples),
    "max": max(samples),
    "samples": samples,
}
stats_path.write_text(json.dumps(stats) + "\n")
PY
}

format_cell() {
  local stats_file="$1" python
  python="$(python_bin)"
  "$python" - "$stats_file" <<'PY'
import json, pathlib, sys

stats = json.loads(pathlib.Path(sys.argv[1]).read_text())

def fmt(value: float) -> str:
    return f"{value:,.0f}"

cell = fmt(stats["median"])
if stats["n"] > 1:
    cell += f" ± {fmt(stats['stdev'])}"
print(cell)
PY
}

run_endpoint() {
  local loop="$1" endpoint="$2"
  local url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
  local samples_file="$RUN_DIR/$loop.$endpoint.samples"
  local stats_file="$RUN_DIR/$loop.$endpoint.stats.json"
  local cmd=("$WRK" -t "$THREADS" -c "$CONNECTIONS" -d "$DURATION" --latency)
  [[ "$endpoint" == validate ]] && cmd+=(-s "$BENCHMARK_DIR/validate.lua")
  : >"$samples_file"
  local total=$((RUNS + WARMUP)) run_idx sample raw_out
  echo "  $endpoint ($CONNECTIONS conn, $RUNS measured + $WARMUP warmup)"
  for ((run_idx = 1; run_idx <= total; run_idx++)); do
    raw_out="$RUN_DIR/$loop.$endpoint.run${run_idx}.txt"
    "${cmd[@]}" "$url" >"$raw_out"
    sample="$(awk '/Requests\/sec:/ {print $2; exit}' "$raw_out")"
    [[ -n "$sample" ]] || { echo "Failed to parse wrk output: $raw_out" >&2; return 1; }
    if ((run_idx > WARMUP)); then
      echo "$sample" >>"$samples_file"
    fi
    if [[ "$KEEP_RAW" != 1 ]]; then
      rm -f "$raw_out"
    fi
  done
  aggregate_samples "$samples_file" "$stats_file"
}

run_loop() {
  local loop="$1" endpoint
  echo
  echo "== stario + $loop =="
  if ! loop_available "$loop"; then
    echo "  skip: package not importable"
    echo "skip" >"$RUN_DIR/$loop.status"
    return 0
  fi
  SERVER_PORT="$(port_for "$loop")"
  require_port_free "$(python_bin)" "$SERVER_PORT"
  echo "  port $SERVER_PORT version=$(pkg_version "$loop")"
  if ! start_server "$loop"; then
    echo "fail" >"$RUN_DIR/$loop.status"
    stop_server
    return 0
  fi
  echo "ok" >"$RUN_DIR/$loop.status"
  for endpoint in "${ENDPOINTS[@]}"; do
    if ! run_endpoint "$loop" "$endpoint"; then
      echo "fail" >"$RUN_DIR/$loop.status"
      break
    fi
  done
  stop_server
}

print_summary() {
    local table="$RUN_DIR/summary.md" loop endpoint stats_file status stario_ver
    stario_ver="$("$(python_bin)" -c "import importlib.metadata as m; print(m.version('stario'))")"
    : >"$table"
    {
    echo "## Stario event-loop comparison"
    echo
    echo "Stario ${stario_ver} only — same checkout, same four HTTP cases, different \`STARIO_LOOP\`."
    echo "Median requests/sec ± sample stdev after discarding $WARMUP warmup run(s)"
    echo "across $RUNS measured samples. wrk -t $THREADS -c $CONNECTIONS -d $DURATION."
    echo
    echo "| Loop | Plaintext | JSON | Params | Validate |"
    echo "| --- | ---: | ---: | ---: | ---: |"
    for loop in "${SELECTED_LOOPS[@]}"; do
      status="$(cat "$RUN_DIR/$loop.status" 2>/dev/null || echo missing)"
      printf '| %s ' "$loop"
      if [[ "$status" != ok ]]; then
        echo "| $status | $status | $status | $status |"
        continue
      fi
      for endpoint in "${ENDPOINTS[@]}"; do
        stats_file="$RUN_DIR/$loop.$endpoint.stats.json"
        if [[ -f "$stats_file" ]]; then
          printf '| %s ' "$(format_cell "$stats_file")"
        else
          printf '| - '
        fi
      done
      echo "|"
    done
    echo
    echo "### Loop packages"
    echo
    echo "| Loop | Version |"
    echo "| --- | --- |"
    for loop in "${SELECTED_LOOPS[@]}"; do
      echo "| $loop | $(pkg_version "$loop") |"
    done
  } | tee "$table"
}

cleanup_raw_outputs() {
  local loop endpoint
  [[ "$KEEP_RAW" == 1 ]] && return 0
  for loop in "${SELECTED_LOOPS[@]}"; do
    [[ -f "$RUN_DIR/$loop.server.log" && "$(cat "$RUN_DIR/$loop.status" 2>/dev/null || true)" == ok ]] && rm -f "$RUN_DIR/$loop.server.log"
    for endpoint in "${ENDPOINTS[@]}"; do
      rm -f "$RUN_DIR/$loop.$endpoint.samples"
    done
  done
}

main() {
  need uv; need curl; need "$WRK"
  trap stop_server EXIT INT TERM

  local selected=("$@") loop
  if ((${#selected[@]} == 0)); then selected=("${ALL_LOOPS[@]}"); fi
  case "${selected[0]}" in -h|--help|help) usage; exit 0 ;; esac
  for loop in "${selected[@]}"; do
    known_loop "$loop" || { echo "Unknown loop: $loop" >&2; usage >&2; exit 1; }
  done
  SELECTED_LOOPS=("${selected[@]}")

  ensure_env

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
payload_file=benchmarks/server/validate.lua
stario_root=$ROOT
stario_sha=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
stario_version=$("$(python_bin)" -c "import importlib.metadata as m; print(m.version('stario'))")
loops=${SELECTED_LOOPS[*]}
EOF
  write_machine_info "$RUN_DIR/config.txt"
  {
    echo
    echo "=== loop versions ==="
    for loop in "${SELECTED_LOOPS[@]}"; do
      echo "$loop=$(pkg_version "$loop")"
    done
  } >>"$RUN_DIR/config.txt"

  echo "Writing results to ${RUN_DIR#$ROOT/}"
  for loop in "${SELECTED_LOOPS[@]}"; do run_loop "$loop"; done
  echo
  print_summary
  cleanup_raw_outputs
  echo
  echo "Summary: ${RUN_DIR#$ROOT/}/summary.md"
}

main "$@"
