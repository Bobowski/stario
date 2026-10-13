#!/usr/bin/env bash
# Full cloud benchmark pass with repeated wrk samples → mean (± stdev).
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="/workspace"
BENCH="$ROOT/benchmarks/server"
ENVS="$BENCH/.venvs-cloud"
REPS="${REPS:-3}"
DURATION="${DURATION:-10s}"
WRK_THREADS="${WRK_THREADS:-2}"
CONNECTIONS="${CONNECTIONS:-128}"
HOST="${HOST:-127.0.0.1}"
BASE_PORT="${BASE_PORT:-3500}"
PYTHON="${PYTHON:-3.14}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BENCH/results-cloud/averaged-$STAMP"
mkdir -p "$OUT"
LOG="$OUT/run.log"
exec > >(tee -a "$LOG") 2>&1

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
need uv; need wrk; need curl

uv python install "$PYTHON" >/dev/null 2>&1 || true
uv python install 3.14t >/dev/null 2>&1 || true

python_for() { echo "$ENVS/$1/bin/python"; }

ensure_env() {
  local name="$1"; shift
  local py; py="$(python_for "$name")"
  if [[ ! -x "$py" ]]; then
    uv venv "$ENVS/$name" --python "$PYTHON"
    uv pip install --python "$py" "$@"
  fi
}

TARGETS=(stario fastapi blacksheep-uvicorn blacksheep-granian sanic falcon robyn)
ENDPOINTS=(plaintext json params validate)

target_offset() {
  local i
  for i in "${!TARGETS[@]}"; do
    [[ "${TARGETS[$i]}" == "$1" ]] && echo "$i" && return 0
  done
  return 1
}

port_for() { echo $((BASE_PORT + $(target_offset "$1"))); }

path_for() {
  case "$1" in
    plaintext) echo /plaintext ;;
    json) echo /json ;;
    params) echo /user/42 ;;
    validate) echo /validate ;;
  esac
}

ensure_all_envs() {
  ensure_env stario stario==5.0.0 uvloop ujson
  ensure_env stario43 stario==4.3.0 uvloop ujson
  ensure_env fastapi fastapi 'uvicorn[standard]' ujson
  ensure_env blacksheep-uvicorn blacksheep 'uvicorn[standard]' ujson
  ensure_env blacksheep-granian blacksheep granian uvloop ujson
  ensure_env sanic sanic uvloop ujson
  ensure_env falcon falcon 'uvicorn[standard]' ujson
  ensure_env robyn robyn ujson
  uv venv "$ENVS/stario-threads" --python 3.14t 2>/dev/null || true
  uv pip install --python "$ENVS/stario-threads/bin/python" stario==5.0.0 uvloop ujson >/dev/null 2>&1 || \
    uv pip install --python "$ENVS/stario-threads/bin/python" stario==5.0.0 uvloop ujson
}

SERVER_PID=""
SERVER_PORT=""

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    local deadline=$((SECONDS + 5))
    while kill -0 "$SERVER_PID" 2>/dev/null && ((SECONDS < deadline)); do
      sleep 0.1
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "${SERVER_PORT:-}" ]] && command -v fuser >/dev/null 2>&1; then
    fuser -k "${SERVER_PORT}/tcp" 2>/dev/null || true
    sleep 0.2
  fi
  SERVER_PID=""
}

wrk_rps() {
  local url="$1" endpoint="$2"
  local -a cmd=(wrk -t "$WRK_THREADS" -c "$CONNECTIONS" -d "$DURATION")
  [[ "$endpoint" == validate ]] && cmd+=(-s "$BENCH/validate.lua")
  "${cmd[@]}" "$url" 2>/dev/null | awk '/Requests\/sec:/ {print $2; exit}'
}

sample_endpoint() {
  local url="$1" endpoint="$2"
  local r vals=() i
  for ((i=0; i<REPS; i++)); do
    r="$(wrk_rps "$url" "$endpoint")"
    vals+=("$r")
    echo "$endpoint rep=$((i+1)) rps=$r" >&2
  done
  printf '%s\n' "${vals[@]}"
}

stats_from_samples() {
  python3 - "$@" <<'PY'
import sys, statistics
vals = [float(x) for x in sys.argv[1:] if x]
if not vals:
    print("0,0")
else:
    m = statistics.mean(vals)
    s = statistics.stdev(vals) if len(vals) > 1 else 0.0
    print(f"{m:.2f},{s:.2f}")
PY
}

start_target() {
  local target="$1"
  local thread_count="${2:-}"
  local pin_threads="${3:-1}"
  if [[ "$target" == "stario-threads" ]]; then
    SERVER_PORT=$((BASE_PORT + 20))
  elif [[ "$target" == "stario43" ]]; then
    SERVER_PORT=$((BASE_PORT + 10))
  else
    SERVER_PORT="$(port_for "$target")"
  fi
  local py log="$OUT/$target.server.log"
  py="$(python_for "$target")"
  stop_server
  case "$target" in
    stario)
      (
        cd "$ROOT"
        export STARIO_HOST="$HOST" STARIO_PORT="$SERVER_PORT" STARIO_TRACER=noop
        export STARIO_COMPRESS_ZSTD_LEVEL=-1 STARIO_COMPRESS_BROTLI_LEVEL=-1 STARIO_COMPRESS_GZIP_LEVEL=-1
        exec env PYTHONPATH="$BENCH" "$py" -c \
          "import uvloop; from stario import serve; from apps.stario_app import bootstrap; uvloop.run(serve(bootstrap))"
      ) >"$log" 2>&1 &
      ;;
    stario43)
      (
        cd "$ROOT"
        export STARIO_HOST="$HOST" STARIO_PORT="$SERVER_PORT" STARIO_TRACER=noop
        export STARIO_COMPRESS_ZSTD_LEVEL=-1 STARIO_COMPRESS_BROTLI_LEVEL=-1 STARIO_COMPRESS_GZIP_LEVEL=-1
        exec env PYTHONPATH="$BENCH" "$py" -c \
          "import uvloop; from stario import serve; from stario.http.config import server_config_from_env; import importlib.util; spec=importlib.util.spec_from_file_location('a','$ROOT/benchmarks/server/.stario43_app.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); uvloop.run(serve(m.bootstrap, config=server_config_from_env()))"
      ) >"$log" 2>&1 &
      ;;
    fastapi)
      (cd "$ROOT" && exec env PYTHONPATH="$BENCH" "$py" -m uvicorn apps.fastapi_app:app \
        --host "$HOST" --port "$SERVER_PORT" --workers 1 --loop uvloop --http httptools --no-access-log --log-level warning) >"$log" 2>&1 &
      ;;
    blacksheep-uvicorn)
      (cd "$ROOT" && exec env PYTHONPATH="$BENCH" "$py" -m uvicorn apps.blacksheep_app:app \
        --host "$HOST" --port "$SERVER_PORT" --workers 1 --loop uvloop --http httptools --no-access-log --log-level warning) >"$log" 2>&1 &
      ;;
    blacksheep-granian)
      (cd "$ROOT" && exec env PYTHONPATH="$BENCH" "$py" -m granian apps.blacksheep_app:app \
        --interface asgi --host "$HOST" --port "$SERVER_PORT" --workers 1 --runtime-threads 1 --loop uvloop --no-access-log --log-level warning) >"$log" 2>&1 &
      ;;
    sanic)
      (cd "$ROOT" && exec env PYTHONPATH="$BENCH" "$py" "$BENCH/apps/sanic_app.py" --host "$HOST" --port "$SERVER_PORT") >"$log" 2>&1 &
      ;;
    falcon)
      (cd "$ROOT" && exec env PYTHONPATH="$BENCH" "$py" -m uvicorn apps.falcon_app:app \
        --host "$HOST" --port "$SERVER_PORT" --workers 1 --loop uvloop --http httptools --no-access-log --log-level warning) >"$log" 2>&1 &
      ;;
    robyn)
      (cd "$ROOT" && exec env PYTHONPATH="$BENCH" "$py" "$BENCH/apps/robyn_app.py" \
        --host "$HOST" --port "$SERVER_PORT" --processes 1 --workers 1 --log-level WARN --disable-openapi) >"$log" 2>&1 &
      ;;
    stario-threads)
      (
        cd "$ROOT"
        export STARIO_HOST="$HOST" STARIO_PORT="$SERVER_PORT" STARIO_THREADS="$thread_count" STARIO_PIN_THREADS="$pin_threads"
        export STARIO_TRACER=noop STARIO_COMPRESS_ZSTD_LEVEL=-1 STARIO_COMPRESS_BROTLI_LEVEL=-1 STARIO_COMPRESS_GZIP_LEVEL=-1
        exec env PYTHONPATH="$BENCH" "$ENVS/stario-threads/bin/python" -c \
          "import uvloop; from stario import serve; from apps.stario_app import bootstrap; uvloop.run(serve(bootstrap))"
      ) >"$log" 2>&1 &
      ;;
    *) echo "unknown target $target" >&2; return 1 ;;
  esac
  SERVER_PID=$!
  local url="http://$HOST:$SERVER_PORT$(path_for plaintext)"
  local deadline=$((SECONDS + 20))
  until curl -fsS --max-time 1 "$url" >/dev/null 2>&1; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "server died: $target" >&2; tail -30 "$log" >&2; return 1
    fi
    ((SECONDS >= deadline)) && { echo "timeout: $target" >&2; tail -20 "$log" >&2; return 1; }
    sleep 0.15
  done
}

run_framework_suite() {
  local target endpoint samples stats mean sd
  echo "## framework_suite" >"$OUT/framework_samples.tsv"
  printf '| Target | Plaintext | JSON | Params | Validate |\n| --- | ---: | ---: | ---: | ---: |\n' >"$OUT/framework_summary.md"
  for target in "${TARGETS[@]}"; do
    echo "== framework: $target =="
    start_target "$target"
    printf '| %s ' "$target" >>"$OUT/framework_summary.md"
    for endpoint in "${ENDPOINTS[@]}"; do
      url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
      mapfile -t samples < <(sample_endpoint "$url" "$endpoint")
      stats="$(stats_from_samples "${samples[@]}")"
      mean="${stats%%,*}"; sd="${stats##*,}"
      printf '%s\t%s\t%s\t%s\t%s\n' "$target" "$endpoint" "$mean" "$sd" "${samples[*]}" >>"$OUT/framework_samples.tsv"
      printf '| %.0f ± %.0f ' "$mean" "$sd" >>"$OUT/framework_summary.md"
      echo "  $target/$endpoint mean=$mean stdev=$sd"
    done
    echo "|" >>"$OUT/framework_summary.md"
    stop_server
    sleep 2
  done
}

run_stario_upgrade() {
  echo "## stario_upgrade" >"$OUT/upgrade_samples.tsv"
  printf '| Endpoint | Stario 4.3.0 | Stario 5.0.0 |\n| --- | ---: | ---: |\n' >"$OUT/upgrade_summary.md"
  local endpoint v43 v50 mean43 mean50
  for endpoint in "${ENDPOINTS[@]}"; do
    for ver in stario43 stario; do
      start_target "$ver"
      url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
      mapfile -t samples < <(sample_endpoint "$url" "$endpoint")
      stats="$(stats_from_samples "${samples[@]}")"
      mean="${stats%%,*}"
      printf '%s\t%s\t%s\n' "$ver" "$endpoint" "$mean" >>"$OUT/upgrade_samples.tsv"
      if [[ "$ver" == "stario43" ]]; then mean43="$mean"; else mean50="$mean"; fi
      stop_server
    done
    printf '| %s | %.0f | %.0f |\n' "$(path_for "$endpoint")" "$mean43" "$mean50" >>"$OUT/upgrade_summary.md"
  done
}

run_html() {
  echo "== html compare =="
  local py="$ENVS/stario-html/bin/python"
  if [[ ! -x "$py" ]]; then
    uv venv "$ENVS/stario-html" --python "$PYTHON"
    uv pip install --python "$py" stario==5.0.0 dominate htpy jinja2 tdom
  fi
  python3 - "$REPS" "$py" "$ROOT/benchmarks/html/compare.py" "$OUT/html_summary.md" <<'PY'
import re, subprocess, statistics, sys
from collections import defaultdict

reps = int(sys.argv[1])
py, script, out_md = sys.argv[2:5]
pat = re.compile(r"^\s+(.+?)\s+(\d+\.?\d*)\s+us")
acc: dict[str, list[float]] = defaultdict(list)
for i in range(reps):
    p = subprocess.run([py, script], cwd="/tmp", capture_output=True, text=True)
    for line in (p.stdout or "").splitlines():
        m = pat.match(line)
        if m:
            acc[m.group(1).strip()].append(float(m.group(2)))
lines = [f"| Renderer | Mean µs (n={reps} runs) |", "| --- | ---: |"]
for name in sorted(acc, key=lambda n: statistics.mean(acc[n])):
    vals = acc[name]
    lines.append(f"| {name} | {statistics.mean(vals):.1f} |")
    print(f"  html {name} mean={statistics.mean(vals):.1f}")
open(out_md, "w").write("\n".join(lines) + "\n")
PY
}

run_threads() {
  echo "## threads" >"$OUT/threads_samples.tsv"
  printf '| Threads | Plaintext (mean ± σ) | JSON (mean ± σ) |\n| ---: | ---: | ---: |\n' >"$OUT/threads_summary.md"
  local th endpoint samples stats mean sd
  WRK_THREADS=1
  CONNECTIONS=128
  DURATION=15s
  for th in 1 2 4; do
    echo "== threads: $th =="
    start_target stario-threads "$th" 1
    for endpoint in plaintext json; do
      url="http://$HOST:$SERVER_PORT$(path_for "$endpoint")"
      mapfile -t samples < <(sample_endpoint "$url" "$endpoint")
      stats="$(stats_from_samples "${samples[@]}")"
      mean="${stats%%,*}"; sd="${stats##*,}"
      printf '%s\t%s\t%s\t%s\n' "$th" "$endpoint" "$mean" "$sd" >>"$OUT/threads_samples.tsv"
      if [[ "$endpoint" == plaintext ]]; then pt_m="$mean"; pt_s="$sd"; else js_m="$mean"; js_s="$sd"; fi
    done
    printf '| %s | %.0f ± %.0f | %.0f ± %.0f |\n' "$th" "$pt_m" "$pt_s" "$js_m" "$js_s" >>"$OUT/threads_summary.md"
    stop_server
  done
  WRK_THREADS=2
  CONNECTIONS=128
  DURATION=10s
}

write_report() {
  cat >"$OUT/REPORT.md" <<EOF
# Stario benchmarks — averaged cloud run ($STAMP)

**Method:** Each HTTP endpoint: **$REPS** wrk runs (\`-t$WRK_THREADS -c$CONNECTIONS -d$DURATION\`), report **mean ± stdev** (RPS).  
**Host:** $HOST · **Python:** $PYTHON · **Stario:** PyPI 5.0.0 (framework suite)  
**Note:** Shared 4-vCPU cloud VM; averages dampen run-to-run noise but not neighbor load.

### System
See \`../system-info.txt\` and \`run.log\`.

### Framework comparison (mean ± σ RPS)

$(cat "$OUT/framework_summary.md")

### Stario 4.3.0 vs 5.0.0 (mean RPS per endpoint)

$(cat "$OUT/upgrade_summary.md")

### HTML render (mean µs over $REPS runs of compare.py)

$(cat "$OUT/html_summary.md")

### Free-threaded \`STARIO_THREADS\` (3.14t, \`STARIO_PIN_THREADS=1\`, wrk \`-t1 -c128 -d15s\`)

$(cat "$OUT/threads_summary.md")

Raw TSV: \`framework_samples.tsv\`, \`upgrade_samples.tsv\`, \`threads_samples.tsv\`.
EOF
  cp "$OUT/REPORT.md" "$BENCH/results-cloud/AVERAGED-LATEST.md"
  ln -sfn "$OUT" "$BENCH/results-cloud/averaged-latest"
  echo "Report: $OUT/REPORT.md"
}

# Stario 4.3 app (4.x API)
if [[ ! -f "$BENCH/.stario43_app.py" ]]; then
  git show 4.3.0:benchmarks/server/apps/stario_app.py >"$BENCH/.stario43_app.py"
fi

{
  echo "reps=$REPS duration=$DURATION wrk_threads=$WRK_THREADS connections=$CONNECTIONS"
  echo "base_port=$BASE_PORT stamp=$STAMP"
} >"$OUT/config.txt"

trap stop_server EXIT INT TERM
ensure_all_envs
run_framework_suite
run_stario_upgrade
run_html
run_threads
write_report
