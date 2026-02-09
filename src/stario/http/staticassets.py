"""
StaticAssets - Serve static files with fingerprinted URLs.

Global asset() function for easy URL lookup from anywhere.

Features:
- Fingerprinted URLs for cache busting
- In-memory caching for small files (< 1MB by default)
- Pre-compression (zstd, brotli, gzip) at startup
- Large file streaming with aiofiles
- Immutable cache headers

Usage:
    from stario.http.staticassets import StaticAssets, asset

    # Create and mount (registers to global lookup)
    static = StaticAssets("./static", collection="static")
    app.get("/static/*", static)

    # Use anywhere in your code
    asset("style.css")  # "style.abc123.css"
    asset("js/app.js")  # "js/app.def456.js"

    # Multiple collections
    uploads = StaticAssets("./uploads", collection="uploads")
    asset("photo.jpg", collection="uploads")
"""

import zlib
from compression import zstd
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import aiofiles
import brotli
import xxhash

from stario.exceptions import StarioError

from .types import Context
from .writer import Writer

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

_DEFAULT_CHUNK_SIZE: Final = 4 << 20
_DEFAULT_CACHE_MAX_SIZE: Final = 1 << 20  # 1MB
_DEFAULT_COMPRESS_MIN_SIZE: Final = 256  # Don't compress tiny files
_DEFAULT_FILESYSTEM_CHUNK_SIZE: Final = 65536

# Global registry: collection name -> StaticAssets instance
_collections: dict[str, "StaticAssets"] = {}


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


def asset(filename: str, collection: str = "static") -> str:
    """
    Get the fingerprinted filename for a static asset.

    Args:
        filename: Original filename relative to collection root
        collection: Collection name (default "static")

    Returns:
        Fingerprinted filename (e.g., "style.abc123.css")

    Raises:
        KeyError: If collection is not registered
        ValueError: If file not found in collection

    Example:
        asset("style.css")  # "style.abc123.css"
        asset("js/app.js")  # "js/app.def456.js"
        asset("photo.jpg", collection="uploads")
    """
    if collection not in _collections:
        raise KeyError(
            f"Collection '{collection}' not registered. "
            f"Available: {list(_collections.keys())}"
        )
    return _collections[collection].url(filename)


def fingerprint(path: Path, *, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> str:
    """Generate a 16-character hash of file contents using xxHash64."""
    hasher = xxhash.xxh64()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


class StaticAssets:
    """
    Serve static files with fingerprinted URLs.

    Features:
    - Files hashed at startup for cache-busting URLs
    - Small files (< cache_max_size) cached in memory
    - Pre-compression (zstd, brotli, gzip) for text-based files
    - Immutable cache headers for aggressive browser caching

    Usage:
        static = StaticAssets("./static")
        app.get("/static/*", static)

        # In templates/anywhere:
        from stario.http.staticassets import asset
        asset("style.css")  # "style.abc123.css"
    """

    __slots__ = (
        "directory",
        "collection",
        "cache_max_size",
        "_cache_control_bytes",
        "_path_to_hash",
        "_cache",
    )

    def __init__(
        self,
        directory: Path | str = "./static",
        collection: str = "static",
        cache_control: str = "public, max-age=31536000, immutable",
        cache_max_size: int = _DEFAULT_CACHE_MAX_SIZE,
    ) -> None:
        self.directory = Path(directory).resolve()
        self.collection = collection
        self.cache_max_size = cache_max_size
        self._cache_control_bytes = cache_control.encode()

        if not self.directory.is_dir():
            raise StarioError(
                f"Static files directory not found: {self.directory}",
                context={
                    "path": str(self.directory),
                    "exists": self.directory.exists(),
                },
                help_text="Create the directory or check the path in your app.assets() call.",
                example='app.assets("/static", Path(__file__).parent / "static")',
            )

        self._path_to_hash: dict[str, str] = {}
        self._cache: dict[str, CachedFile] = {}

        for p in self.directory.rglob("*"):
            if not p.is_file():
                continue

            hashed_name = f"{p.stem}.{fingerprint(p)}{p.suffix}"
            relative_path = p.relative_to(self.directory)
            hashed_path = relative_path.with_name(hashed_name)
            hashed_key = hashed_path.as_posix()

            self._path_to_hash[relative_path.as_posix()] = hashed_name
            self._cache[hashed_key] = self._create_cached_file(p)

        # Register to global lookup
        _collections[collection] = self

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
            or size < _DEFAULT_COMPRESS_MIN_SIZE
        ):
            return CachedFile(size=size, content_type=content_type, content=content)

        # Pre-compress with available algorithms
        zstd_data = zstd.compress(content, level=3)
        brotli_data = brotli.compress(content, quality=4)
        cobj = zlib.compressobj(6, zlib.DEFLATED, 31)
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

        if f.zstd and b"zstd" in accept:
            return f.zstd, b"zstd"
        if f.brotli and b"br" in accept:
            return f.brotli, b"br"
        if f.gzip and b"gzip" in accept:
            return f.gzip, b"gzip"
        return f.content, None

    async def __call__(self, c: Context, w: Writer) -> None:
        """Serve a static file."""
        tail = c.req.tail or ""

        # Security: Path traversal is prevented by design - we only serve files that
        # exist in our pre-built _cache dict, which was populated at startup by
        # iterating self.directory. The cache keys are normalized relative paths,
        # so "../../../etc/passwd" simply won't exist as a cache key.
        # Additionally, any ".." in the URL path is already handled by the
        # HTTP parser before reaching here.

        # Try fingerprinted path first (cache hit)
        f = self._cache.get(tail)

        # Try original path → redirect to fingerprinted
        if f is None and (hashed := self._path_to_hash.get(tail)):
            # Use 307 (not 301) to avoid browser caching stale redirects
            # When file changes, hash changes, so redirect destination changes
            w.redirect(hashed, 307)
            return

        if f is None:
            w.text("Not Found", 404)
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
                while chunk := await fp.read(_DEFAULT_FILESYSTEM_CHUNK_SIZE):
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
            h.rset(b"vary", b"accept-encoding")

        h.rset(b"content-length", b"%d" % len(body))

        if c.req.method == "HEAD":
            w.write_headers(200).end()
            return

        w.write_headers(200).end(body)

    def url(self, filename: str) -> str:
        """
        Get the fingerprinted filename for a file.

        Args:
            filename: Original filename relative to directory

        Returns:
            Fingerprinted filename with path preserved
            e.g., "js/app.js" -> "js/app.abc123.js"
        """
        filename = filename.strip("/")
        if hashed_name := self._path_to_hash.get(filename):
            parts = filename.rsplit("/", 1)
            if len(parts) == 2:
                return f"{parts[0]}/{hashed_name}"
            return hashed_name

        available = list(self._path_to_hash.keys())[:5]
        raise StarioError(
            f"Static file not found: '{filename}'",
            context={"filename": filename, "collection": self.collection},
            help_text=f"Available files: {available}{'...' if len(self._path_to_hash) > 5 else ''}",
        )
