#!/usr/bin/env bash
# HTML compare + micro on main and cython-core (event-loop independent).
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$HERE/../.." && pwd)"
MAIN_ROOT="${MAIN_ROOT:-$WORKSPACE}"
CYTHON_ROOT="${CYTHON_ROOT:-/tmp/stario-cython}"
OUT_DIR="${OUT_DIR:-$HERE/results/html}"
PYTHON="${PYTHON:-3.14}"

mkdir -p "$OUT_DIR"

run_tree() {
  local name="$1" root="$2"
  echo "== HTML $name ($root) =="
  (
    cd "$root"
    uv run --python "$PYTHON" --with dominate --with htpy --with jinja2 --with tdom \
      benchmarks/html/compare.py
  ) | tee "$OUT_DIR/${name}-compare.txt"
  (
    cd "$root"
    uv run --python "$PYTHON" benchmarks/html/micro.py
  ) | tee "$OUT_DIR/${name}-micro.txt"
}

run_tree main "$MAIN_ROOT"
run_tree cython-core "$CYTHON_ROOT"

if [[ -f "$CYTHON_ROOT/benchmarks/headers_micro.py" && -x "$HERE/.venvs/cython/bin/python" ]]; then
  echo "== headers_micro (cython-core) =="
  (
    cd "$CYTHON_ROOT"
    HEADERS_BENCH_JSON="$OUT_DIR/headers-micro.json" \
      PYTHONPATH=src:. "$HERE/.venvs/cython/bin/python" benchmarks/headers_micro.py
  ) | tee "$OUT_DIR/headers-micro.txt"
fi
