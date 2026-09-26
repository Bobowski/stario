"""Build the Cython HTTP runtime (``stario_cython``).

Native libraries come from pkg-config: nghttp2 1.66+ (MadeYouReset,
CVE-2025-8671) and Brotli. Gzip links system zlib. llhttp is vendored.
A distro nghttp2 older than 1.66 that carries the security backports can be
used with ``STARIO_ALLOW_OLD_NGHTTP2=1``.

Development build: ``uv pip install -e .`` or ``python setup.py build_ext --inplace``.
"""

import os
import platform
import shlex
import subprocess

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

_NATIVE_PACKAGES = ("libnghttp2", "libbrotlienc", "libbrotlicommon")
_MIN_NGHTTP2 = (1, 66)


def _pkg_config(option: str) -> list[str]:
    try:
        result = subprocess.run(
            ["pkg-config", option, *_NATIVE_PACKAGES],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(
            "Building stario needs pkg-config plus the nghttp2 and Brotli "
            "development packages (Debian/Ubuntu: libnghttp2-dev libbrotli-dev; "
            "macOS: brew install pkg-config nghttp2 brotli). "
            f"pkg-config detail: {detail.strip()}"
        ) from exc
    return shlex.split(result.stdout)


def _require_nghttp2_version() -> None:
    if os.environ.get("STARIO_ALLOW_OLD_NGHTTP2") == "1":
        return
    raw = subprocess.run(
        ["pkg-config", "--modversion", "libnghttp2"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    version = tuple(int(part) for part in raw.split(".")[:2] if part.isdigit())
    if version < _MIN_NGHTTP2:
        wanted = ".".join(map(str, _MIN_NGHTTP2))
        raise RuntimeError(
            f"stario needs nghttp2 {wanted}+ (found {raw}): older releases lack "
            "the MadeYouReset (CVE-2025-8671) fix. Install a newer libnghttp2, "
            "build one with scripts/build-native-deps.sh, or set "
            "STARIO_ALLOW_OLD_NGHTTP2=1 if your distro backports the fix."
        )


class native_build_ext(build_ext):
    """Resolve pkg-config flags at compile time so sdist/metadata need no libs."""

    def build_extensions(self) -> None:
        _pkg_config("--exists")
        _require_nghttp2_version()
        include_dirs = [f[2:] for f in _pkg_config("--cflags-only-I") if f.startswith("-I")]
        library_dirs = [f[2:] for f in _pkg_config("--libs-only-L") if f.startswith("-L")]
        libraries = [f[2:] for f in _pkg_config("--libs-only-l") if f.startswith("-l")]
        if "z" not in libraries:
            libraries.append("z")
        compile_args = _pkg_config("--cflags-only-other")
        link_args = _pkg_config("--libs-only-other")
        for ext in self.extensions:
            ext.include_dirs.extend(include_dirs)
            ext.library_dirs.extend(library_dirs)
            ext.libraries.extend(libraries)
            ext.extra_compile_args.extend(compile_args)
            ext.extra_link_args.extend(link_args)
        super().build_extensions()


base_args = ["-O3", "-fno-strict-aliasing"]
llhttp_sse = ["-msse4.2"] if platform.machine().lower() in {"x86_64", "amd64"} else []
include_dirs = ["vendor", "vendor/llhttp/include", "src"]

extensions = [
    Extension(
        "stario_cython.exchange",
        sources=["src/stario_cython/exchange.pyx", "vendor/compression_buf.c"],
        include_dirs=list(include_dirs),
        extra_compile_args=list(base_args),
    ),
    Extension(
        "stario_cython.protocol",
        sources=[
            "src/stario_cython/protocol.pyx",
            "vendor/llhttp/src/llhttp.c",
            "vendor/llhttp/src/http.c",
            "vendor/llhttp/src/api.c",
            "vendor/llhttp/src/stario_alloc.c",
        ],
        include_dirs=list(include_dirs),
        extra_compile_args=[*base_args, *llhttp_sse],
    ),
]

setup(
    ext_modules=cythonize(extensions, compiler_directives={"language_level": 3}),
    cmdclass={"build_ext": native_build_ext},
)
