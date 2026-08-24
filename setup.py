import shlex
import subprocess

from Cython.Build import cythonize
from setuptools import Extension
from setuptools.command.build_ext import build_ext
from setuptools.dist import Distribution

opts = dict(language_level=3)

_CODEC_PACKAGES = ("libbrotlienc", "libbrotlicommon")


def _pkg_config(packages, option):
    try:
        result = subprocess.run(
            ["pkg-config", option, *packages],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(
            "Native codec development libraries are required. "
            "Install pkg-config and brotli. Gzip uses system zlib (-lz). "
            f"pkg-config detail: {detail.strip()}"
        ) from exc
    return shlex.split(result.stdout)


codec_include_dirs = [
    flag[2:] for flag in _pkg_config(_CODEC_PACKAGES, "--cflags-only-I") if flag.startswith("-I")
]
codec_library_dirs = [
    flag[2:] for flag in _pkg_config(_CODEC_PACKAGES, "--libs-only-L") if flag.startswith("-L")
]
codec_libraries = [
    flag[2:] for flag in _pkg_config(_CODEC_PACKAGES, "--libs-only-l") if flag.startswith("-l")
]
if "z" not in codec_libraries:
    codec_libraries.append("z")
codec_compile_args = _pkg_config(_CODEC_PACKAGES, "--cflags-only-other")
codec_link_args = _pkg_config(_CODEC_PACKAGES, "--libs-only-other")

extensions = [
    Extension(
        "stario_cython.headers",
        sources=["src/stario_cython/headers.pyx"],
        extra_compile_args=["-O3"],
    ),
    Extension(
        "stario_cython.request",
        sources=["src/stario_cython/request.pyx"],
        extra_compile_args=["-O3"],
    ),
    Extension(
        "stario_cython.exchange",
        sources=[
            "src/stario_cython/exchange.pyx",
            "vendor/compression_buf.c",
        ],
        include_dirs=["vendor", "src", *codec_include_dirs],
        library_dirs=codec_library_dirs,
        libraries=codec_libraries,
        extra_compile_args=["-O3", *codec_compile_args],
        extra_link_args=codec_link_args,
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
        include_dirs=["vendor/llhttp/include", "vendor", "src"],
        extra_compile_args=["-O3"],
    ),
]

# Do not call setup(): this tree's pyproject.toml belongs to the stario
# uv_build package, and current setuptools rejects its license classifier.
dist = Distribution(
    {
        "ext_modules": cythonize(extensions, **opts),
        "package_dir": {"": "src"},
        "packages": ["stario_cython"],
    }
)
command = build_ext(dist)
command.inplace = True
command.ensure_finalized()
command.run()
