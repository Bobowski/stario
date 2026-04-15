"""
Static files as a route handler: content-hashed filenames plus aggressive caching headers so pages can pin immutable URLs.

Small files may be read into memory (with optional pre-compression); large files stream from disk to keep RAM bounded for
asset-heavy static sites without a separate origin tier.
"""

import zlib
from compression import zstd
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import aiofiles
import brotli
import xxhash

import stario.responses as responses
from stario.exceptions import StarioError

from .context import Context
from .router import Node, _wrap_path_segments, default_not_found
from .writer import Writer, _merge_vary, _parse_accept_encoding

CONTENT_TYPES: dict[str, bytes] = {
    ".html": b"text/html; charset=utf-8",
    ".htm": b"text/html; charset=utf-8",
    ".css": b"text/css; charset=utf-8",
    ".js": b"application/javascript; charset=utf-8",
    ".mjs": b"application/javascript; charset=utf-8",
    ".json": b"application/json; charset=utf-8",
    ".xml": b"application/xml; charset=utf-8",
    ".txt": b"text/plain; charset=utf-8",
    ".md": b"text/markdown; charset=utf-8",
    ".png": b"image/png",
    ".jpg": b"image/jpeg",
    ".jpeg": b"image/jpeg",
    ".gif": b"image/gif",
    ".svg": b"image/svg+xml; charset=utf-8",
    ".ico": b"image/x-icon",
    ".webp": b"image/webp",
    ".avif": b"image/avif",
    ".woff": b"font/woff",
    ".woff2": b"font/woff2",
    ".ttf": b"font/ttf",
    ".otf": b"font/otf",
    ".eot": b"application/vnd.ms-fontobject",
    ".pdf": b"application/pdf",
    ".zip": b"application/zip",
    ".gz": b"application/gzip",
    ".br": b"application/brotli",
    ".mp3": b"audio/mpeg",
    ".mp4": b"video/mp4",
    ".webm": b"video/webm",
    ".wasm": b"application/wasm",
}

_DEFAULT_CONTENT_TYPE: Final = b"application/octet-stream"

# File types that are already compressed (skip pre-compression)
_PRECOMPRESSED_EXTENSIONS: Final = frozenset(
    {
        ".gz",
        ".br",
        ".zst",  # compressed
        ".zip",
        ".7z",
        ".rar",
        ".tar",  # archives
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",  # images
        ".mp3",
        ".mp4",
        ".webm",
        ".ogg",
        ".flac",  # media
        ".woff",
        ".woff2",  # fonts (already compressed)
        ".pdf",  # usually compressed internally
    }
)


@dataclass(slots=True)
class CachedFile:
    """
    Cached file entry.

    For small files: content + pre-compressed variants loaded in memory.
    For large files: only metadata, content read from disk on demand.
    """

    size: int
    content_type: bytes
    # None = large file, read from disk
    content: bytes | None = None
    # Path for large files (disk read on demand)
    path: Path | None = None
    # Pre-compressed variants (None if not worth compressing or large file)
    zstd: bytes | None = None
    brotli: bytes | None = None
    gzip: bytes | None = None


def fingerprint(path: Path, *, chunk_size: int = 4 << 20) -> str:
    """xxHash64 of file bytes (hex digest) for fingerprinted static URLs."""
    hasher = xxhash.xxh64()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


class StaticAssets:
    """
    Scan a directory at init: fingerprint filenames, cache small files (optional pre-compression), stream large files.

    Mount with ``app.mount("/static", static)``; exposes ``root`` / ``named_routes`` like a router subtree. Non-fingerprint paths 307 to hashed URLs.
    """

    __slots__ = (
        "directory",
        "name",
        "named_routes",
        "cache_max_size",
        "hash_chunk_size",
        "compress_min_size",
        "filesystem_chunk_size",
        "zstd_level",
        "zstd_window_log",
        "brotli_level",
        "brotli_window_log",
        "gzip_level",
        "gzip_window_bits",
        "_cache_control_bytes",
        "_path_to_hash",
        "_cache",
        "root",
    )

    def __init__(
        self,
        directory: Path | str = "./static",
        *,
        cache_control: str = "public, max-age=31536000, immutable",
        cache_max_size: int = 1 << 20,
        name: str | None = None,
        hash_chunk_size: int = 4 << 20,
        compress_min_size: int = 256,
        filesystem_chunk_size: int = 65536,
        zstd_level: int = 9,
        zstd_window_log: int = 21,
        brotli_level: int = 9,
        brotli_window_log: int = 22,
        gzip_level: int = 7,
        gzip_window_bits: int = 15,
    ) -> None:
        self.directory = Path(directory).resolve()
        self.name = name
        self.cache_max_size = cache_max_size
        self.hash_chunk_size = hash_chunk_size
        self.compress_min_size = compress_min_size
        self.filesystem_chunk_size = filesystem_chunk_size
        self.zstd_level = zstd_level
        self.zstd_window_log = zstd_window_log
        self.brotli_level = brotli_level
        self.brotli_window_log = brotli_window_log
        self.gzip_level = gzip_level
        self.gzip_window_bits = gzip_window_bits
        self._cache_control_bytes = cache_control.encode("ascii")

        if not self.directory.is_dir():
            raise StarioError(
                f"Static files directory not found: {self.directory}",
                context={
                    "path": str(self.directory),
                    "exists": self.directory.exists(),
                },
                help_text="Create the directory or check the path before mounting StaticAssets.",
                example='app.mount("/static", StaticAssets(Path(__file__).parent / "static", name="static"))',
            )

        self._path_to_hash: dict[str, str] = {}
        self._cache: dict[str, CachedFile] = {}

        for p in self.directory.rglob("*"):
            if not p.is_file():
                continue

            hashed_name = (
                f"{p.stem}.{fingerprint(p, chunk_size=self.hash_chunk_size)}{p.suffix}"
            )
            relative_path = p.relative_to(self.directory)
            hashed_path = relative_path.with_name(hashed_name)
            hashed_key = hashed_path.as_posix()

            self._path_to_hash[relative_path.as_posix()] = hashed_key
            self._cache[hashed_key] = self._create_cached_file(p)

        if self.name is None:
            named_routes = {}
        else:
            named_routes = {
                f"{self.name}:{relative_path}": f"/{hashed_path}"
                for relative_path, hashed_path in self._path_to_hash.items()
            }
        self.named_routes = MappingProxyType(named_routes)

        root = Node(
            kind="path",
            not_found_handler=default_not_found,
            exact={
                "GET": Node(
                    kind="method",
                    route_handler=self,
                    not_found_handler=default_not_found,
                ),
                "HEAD": Node(
                    kind="method",
                    route_handler=self,
                    not_found_handler=default_not_found,
                ),
            },
        )
        self.root = _wrap_path_segments(root, ["{path...}"], default_not_found)

    def _create_cached_file(self, path: Path) -> CachedFile:
        """Create cached file entry - in-memory for small, metadata-only for large."""
        size = path.stat().st_size
        content_type = CONTENT_TYPES.get(path.suffix, _DEFAULT_CONTENT_TYPE)

        # Large file: store metadata only, read from disk on demand
        if size > self.cache_max_size:
            return CachedFile(size=size, content_type=content_type, path=path.resolve())

        # Small file: load into memory
        content = path.read_bytes()

        # Skip compression for already-compressed file types or tiny files
        if (
            path.suffix.lower() in _PRECOMPRESSED_EXTENSIONS
            or size < self.compress_min_size
        ):
            return CachedFile(size=size, content_type=content_type, content=content)

        # Startup compression can spend a little more CPU to shrink long-lived assets.
        zstd_data = zstd.compress(
            content,
            options={
                zstd.CompressionParameter.compression_level: self.zstd_level,
                zstd.CompressionParameter.window_log: self.zstd_window_log,
            },
        )
        brotli_data = brotli.compress(
            content,
            quality=self.brotli_level,
            lgwin=self.brotli_window_log,
        )
        cobj = zlib.compressobj(
            self.gzip_level,
            zlib.DEFLATED,
            16 + self.gzip_window_bits,
        )
        gzip_data = cobj.compress(content) + cobj.flush()

        return CachedFile(
            size=size,
            content_type=content_type,
            content=content,
            zstd=zstd_data if len(zstd_data) < size else None,
            brotli=brotli_data if len(brotli_data) < size else None,
            gzip=gzip_data if len(gzip_data) < size else None,
        )

    def _select_body(self, f: CachedFile, accept: bytes) -> tuple[bytes, bytes | None]:
        """
        Select best body variant based on Accept-Encoding.

        Returns:
            Tuple of (body_bytes, encoding) where encoding is None for uncompressed.
        """
        assert f.content is not None, "Cannot select body for large file"

        if not accept:
            return f.content, None

        try:
            header = accept.decode("latin-1")
        except UnicodeDecodeError:
            return f.content, None

        q = _parse_accept_encoding(header)

        def qtok(tok: str) -> float:
            return max(0.0, min(1.0, q.get(tok, q.get("*", 0.0))))

        candidates: list[tuple[float, bytes, bytes]] = []
        if f.brotli:
            candidates.append((qtok("br"), b"br", f.brotli))
        if f.zstd:
            candidates.append((qtok("zstd"), b"zstd", f.zstd))
        if f.gzip:
            candidates.append((qtok("gzip"), b"gzip", f.gzip))

        if not candidates:
            return f.content, None

        best_q, enc, body = max(candidates, key=lambda x: x[0])
        if best_q <= 0.0:
            return f.content, None
        return body, enc

    async def __call__(self, c: Context, w: Writer) -> None:
        """GET/HEAD handler: resolve ``{path...}`` against the startup index, redirect, 404, or send bytes from memory or disk."""
        path = c.route.params.get("path", "").strip("/")

        # Security: Path traversal is prevented by design - we only serve files that
        # exist in our pre-built _cache dict, which was populated at startup by
        # iterating self.directory. The cache keys are normalized relative paths,
        # so "../../../etc/passwd" simply won't exist as a cache key.
        # Additionally, any ".." in the URL path is already handled by the
        # HTTP parser before reaching here.

        # Try fingerprinted path first (cache hit)
        f = self._cache.get(path)

        # Try original path → redirect to fingerprinted
        if f is None and (hashed := self._path_to_hash.get(path)):
            # Use 307 (not 301) to avoid browser caching stale redirects
            # When file changes, hash changes, so redirect destination changes
            #
            # Derive the absolute-path redirect from the request URL so that
            # subdirectory assets redirect correctly.  e.g. requesting
            # "/static/js/app.js" (path="js/app.js") redirects to
            # "/static/js/app.abc123.js", not the relative "app.abc123.js".
            req_path = c.req.path
            responses.redirect(w, req_path[: -len(path)] + hashed, 307)
            return

        if f is None:
            responses.text(w, "Not Found", 404)
            return

        # Serve file - from memory cache or disk
        h = w.headers
        h.rset(b"cache-control", self._cache_control_bytes)
        h.rset(b"content-type", f.content_type)

        # Large file: no compression, serve from disk
        if f.content is None:
            assert f.path is not None, "Large file must have a path"

            h.rset(b"content-length", b"%d" % f.size)

            if c.req.method == "HEAD":
                w.write_headers(200).end()
                return

            w.write_headers(200)

            async with aiofiles.open(f.path, "rb") as fp:
                while chunk := await fp.read(self.filesystem_chunk_size):
                    if w.disconnected:
                        return
                    w.write(chunk)

            w.end()
            return

        # Small file: serve from memory with content negotiation
        accept = c.req.headers.rget(b"accept-encoding", b"").lower()
        body, encoding = self._select_body(f, accept)

        if encoding:
            h.rset(b"content-encoding", encoding)
            _merge_vary(h, b"accept-encoding")

        h.rset(b"content-length", b"%d" % len(body))

        if c.req.method == "HEAD":
            w.write_headers(200).end()
            return

        w.write_headers(200).end(body)
