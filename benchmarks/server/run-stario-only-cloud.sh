#!/usr/bin/env bash
# Stario 5.0.0 only — PyPI wheel on **Python 3.14t** (3.14 GIL ~45k RPS here; 3.14t ~240k).
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="/workspace"
BENCH="$ROOT/benchmarks/server"
ENVS="$BENCH/.venvs-cloud"
REPS="${REPS:-5}"
DURATION="${DURATION:-10s}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3620}"
ENDPOINTS=(plaintext json params validate)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BENCH/results-cloud/stario-only-$STAMP"
mkdir -p "$OUT"

PY="$ENVS/stario-314t/bin/python"
if [[ ! -x "$PY" ]]; then
  uv python install 3.14t >/dev/null
  uv venv "$ENVS/stario-314t" --python 3.14t
  uv pip install --python "$PY" stario==5.0.0 uvloop ujson
fi

path_for() {
  case "$1" in
    plaintext) echo /plaintext ;;
    json) echo /json ;;
    params) echo /user/42 ;;
    validate) echo /validate ;;
  esac
}

fuser -k "${PORT}/tcp" 2>/dev/null || true
sleep 0.3
(
  cd "$ROOT"
  export STARIO_HOST="$HOST" STARIO_PORT="$PORT" STARIO_TRACER=noop
  export STARIO_COMPRESS_ZSTD_LEVEL=-1 STARIO_COMPRESS_BROTLI_LEVEL=-1 STARIO_COMPRESS_GZIP_LEVEL=-1
  exec env PYTHONPATH="$BENCH" "$PY" -c \
    "import uvloop; from stario import serve; from apps.stario_app import bootstrap; uvloop.run(serve(bootstrap))"
) >"$OUT/server.log" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null; fuser -k ${PORT}/tcp 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  curl -fsS --max-time 0.5 "http://${HOST}:${PORT}/plaintext" >/dev/null 2>&1 && break
  sleep 0.1
done

{
  echo "# Stario 5.0.0 only — $STAMP"
  echo "python=$("$PY" -V 2>&1) gil=$("$PY" -c 'import sys; print(sys._is_gil_enabled())')"
  echo "wrk -t2 -c128 -d$DURATION reps=$REPS"
  echo
} | tee "$OUT/summary.md"

printf '| Endpoint | Mean RPS | ± σ |\n| --- | ---: | ---: |\n' >>"$OUT/summary.md"

for endpoint in "${ENDPOINTS[@]}"; do
  url="http://${HOST}:${PORT}$(path_for "$endpoint")"
  samples=()
  for ((i=1; i<=REPS; i++)); do
    if [[ "$endpoint" == validate ]]; then
      r=$(wrk -t2 -c128 -d "$DURATION" -s "$BENCH/validate.lua" "$url" 2>/dev/null | awk '/Requests\/sec:/ {print $2; exit}')
    else
      r=$(wrk -t2 -c128 -d "$DURATION" "$url" 2>/dev/null | awk '/Requests\/sec:/ {print $2; exit}')
    fi
    samples+=("$r")
    echo "$endpoint rep=$i rps=$r" | tee -a "$OUT/samples.tsv"
  done
  read -r mean sd < <(python3 - "${samples[@]}" <<'PY'
import sys, statistics
v=[float(x) for x in sys.argv[1:]]
print(statistics.mean(v), statistics.stdev(v) if len(v)>1 else 0)
PY
)
  printf '| %s | %.0f | %.0f |\n' "$(path_for "$endpoint")" "$mean" "$sd" | tee -a "$OUT/summary.md"
done

echo "Wrote $OUT/summary.md"
