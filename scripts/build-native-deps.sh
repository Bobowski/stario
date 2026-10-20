#!/usr/bin/env bash
# Build the native libraries stario links (nghttp2, Brotli, zstd) into PREFIX.
# Used by cibuildwheel: manylinux ships an nghttp2 without
# nghttp2_option_set_max_continuations, and Homebrew bottles target the
# runner's macOS rather than MACOSX_DEPLOYMENT_TARGET.
set -euo pipefail

PREFIX="${1:-/usr/local}"
NGHTTP2_VERSION="1.70.0"
BROTLI_VERSION="1.2.0"
ZSTD_VERSION="1.5.7"
# nghttp2: GitHub release asset digest. zstd: published .sha256 next to the
# tarball. brotli publishes no hash for tag archives — pinned on first use.
NGHTTP2_SHA256="e05cb1388eaca3830aded4ccf20044b6e1ac1a61411dcca11b0437c4285c8bc2"
BROTLI_SHA256="816c96e8e8f193b40151dad7e8ff37b1221d019dbcb9c35cd3fadbfe6477dfec"
ZSTD_SHA256="eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

verify_sha256() {
  local file="$1" expected="$2" actual
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$file" | cut -d' ' -f1)"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$file" | cut -d' ' -f1)"
  else
    echo "no SHA-256 tool (need sha256sum or shasum)" >&2
    exit 1
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "SHA-256 mismatch for $file: expected $expected, got $actual" >&2
    exit 1
  fi
}

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
verify_sha256 nghttp2.tar.xz "$NGHTTP2_SHA256"
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
verify_sha256 brotli.tar.gz "$BROTLI_SHA256"
tar xf brotli.tar.gz
cmake -S "brotli-${BROTLI_VERSION}" -B brotli-build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DBROTLI_DISABLE_TESTS=ON >/dev/null
cmake --build brotli-build -j"$JOBS" >/dev/null
cmake --install brotli-build >/dev/null

curl -fsSL -o zstd.tar.gz \
  "https://github.com/facebook/zstd/releases/download/v${ZSTD_VERSION}/zstd-${ZSTD_VERSION}.tar.gz"
verify_sha256 zstd.tar.gz "$ZSTD_SHA256"
tar xf zstd.tar.gz
cmake -S "zstd-${ZSTD_VERSION}/build/cmake" -B zstd-build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DZSTD_BUILD_PROGRAMS=OFF \
  -DZSTD_BUILD_TESTS=OFF \
  -DZSTD_BUILD_STATIC=OFF \
  -DZSTD_BUILD_SHARED=ON >/dev/null
cmake --build zstd-build -j"$JOBS" >/dev/null
cmake --install zstd-build >/dev/null

PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" pkg-config --modversion libnghttp2 libbrotlienc libzstd
