#!/usr/bin/env bash
# Build the native libraries stario links (nghttp2, Brotli) into PREFIX.
# Used by cibuildwheel: manylinux ships an nghttp2 without
# nghttp2_option_set_max_continuations, and Homebrew bottles target the
# runner's macOS rather than MACOSX_DEPLOYMENT_TARGET.
set -euo pipefail

PREFIX="${1:-/usr/local}"
NGHTTP2_VERSION="1.70.0"
BROTLI_VERSION="1.2.0"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

SUDO=""
if [[ ! -w "$(dirname "$PREFIX")" && "$(id -u)" != 0 ]]; then
  SUDO="sudo"
fi
$SUDO mkdir -p "$PREFIX"
$SUDO chown "$(id -u)" "$PREFIX" 2>/dev/null || true

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

curl -fsSL -o nghttp2.tar.xz \
  "https://github.com/nghttp2/nghttp2/releases/download/v${NGHTTP2_VERSION}/nghttp2-${NGHTTP2_VERSION}.tar.xz"
tar xf nghttp2.tar.xz
(
  cd "nghttp2-${NGHTTP2_VERSION}"
  ./configure --prefix="$PREFIX" --libdir="$PREFIX/lib" --enable-lib-only \
    --disable-static --disable-dependency-tracking >/dev/null
  make -j"$JOBS" >/dev/null
  make install >/dev/null
)

curl -fsSL -o brotli.tar.gz \
  "https://github.com/google/brotli/archive/refs/tags/v${BROTLI_VERSION}.tar.gz"
tar xf brotli.tar.gz
cmake -S "brotli-${BROTLI_VERSION}" -B brotli-build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DBROTLI_DISABLE_TESTS=ON >/dev/null
cmake --build brotli-build -j"$JOBS" >/dev/null
cmake --install brotli-build >/dev/null

PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" pkg-config --modversion libnghttp2 libbrotlienc
